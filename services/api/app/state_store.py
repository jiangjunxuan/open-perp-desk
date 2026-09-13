import json
import math
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .database_maintenance import recovery_marker
from .historical_ledger import DAY_MS, combine_history_windows, day_ms
from .strategy_engine import DEFAULT_STRATEGY_CONFIG


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


ACTIVE_ORDER_STATUSES = (
    "preparing", "submitting", "submitted", "pending", "accepted", "live",
    "partially_filled", "submission_unknown", "unknown", "cancel_failed", "canceling",
)
TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "failed", "rejected",
    "effective", "triggered", "order_failed", "expired", "mmp_canceled",
}


class ExposureSnapshotChanged(RuntimeError):
    """A different submission reserved risk budget after the snapshot was read."""


class OrderSnapshotConflict(ValueError):
    """Exchange evidence does not match the durable order identity."""


class BillImportBusy(RuntimeError):
    """The account already has an import with a current lease."""


class BillImportLeaseLost(RuntimeError):
    """A stopped or superseded importer cannot publish more data."""


class StateStore:
    """Small durable store for the control plane.

    SQLite keeps the first deployment self-contained. The database path is
    configurable so a Docker volume can persist state without putting secrets
    or runtime data in the repository.
    """

    def __init__(self, path: str | None = None) -> None:
        self.revision = 0
        self._revision_lock = threading.Lock()
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
        if recovery_marker(self.path).exists():
            raise RuntimeError("State database recovery is incomplete; API access is locked.")
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
            if connection.total_changes:
                with self._revision_lock:
                    self.revision += 1
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
                CREATE TABLE IF NOT EXISTS account_bills (
                    account_scope TEXT NOT NULL,
                    bill_id TEXT NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    inst_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY (account_scope, bill_id)
                );
                CREATE INDEX IF NOT EXISTS idx_account_bills_time
                    ON account_bills(account_scope, timestamp_ms DESC);
                CREATE TABLE IF NOT EXISTS account_bill_snapshots (
                    account_scope TEXT PRIMARY KEY,
                    captured_at TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_bill_history (
                    account_scope TEXT NOT NULL,
                    bill_id TEXT NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY (account_scope, bill_id)
                );
                CREATE INDEX IF NOT EXISTS idx_bill_history_cursor
                    ON account_bill_history(account_scope, timestamp_ms DESC, bill_id DESC);
                CREATE TABLE IF NOT EXISTS account_bill_history_windows (
                    account_scope TEXT NOT NULL,
                    day_utc TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    PRIMARY KEY (account_scope, day_utc)
                );
                CREATE TABLE IF NOT EXISTS account_bill_imports (
                    id TEXT PRIMARY KEY,
                    account_scope TEXT NOT NULL,
                    start_day TEXT NOT NULL,
                    end_day TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_days INTEGER NOT NULL,
                    completed_days INTEGER NOT NULL DEFAULT 0,
                    rows_imported INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    lease_until_ms INTEGER NOT NULL,
                    error TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_bill_import
                    ON account_bill_imports(account_scope) WHERE status = 'running';
                CREATE TABLE IF NOT EXISTS account_bill_archives (
                    id TEXT PRIMARY KEY,
                    account_scope TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    quarter TEXT NOT NULL,
                    state TEXT NOT NULL,
                    requested_at_ms INTEGER,
                    next_attempt_ms INTEGER NOT NULL,
                    lease_owner TEXT,
                    lease_until_ms INTEGER NOT NULL DEFAULT 0,
                    import_job_id TEXT,
                    failures INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    UNIQUE(account_scope, year, quarter)
                );
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
                CREATE TABLE IF NOT EXISTS execution_generation (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    generation INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO execution_generation (singleton, generation)
                    VALUES (1, 0);
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
            order_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(orders)")
            }
            if "risk_notional" not in order_columns:
                connection.execute("ALTER TABLE orders ADD COLUMN risk_notional REAL")
            if "account_scope" not in order_columns:
                connection.execute("ALTER TABLE orders ADD COLUMN account_scope TEXT")
            if "order_kind" not in order_columns:
                connection.execute("ALTER TABLE orders ADD COLUMN order_kind TEXT NOT NULL DEFAULT 'standard'")
                connection.execute("UPDATE orders SET order_kind = 'algo' WHERE source LIKE 'okx-algo%'")
            if "exchange_updated_ms" not in order_columns:
                connection.execute("ALTER TABLE orders ADD COLUMN exchange_updated_ms INTEGER")
                for row in connection.execute("SELECT client_order_id, raw_json FROM orders").fetchall():
                    try:
                        timestamp = int(json.loads(row["raw_json"]).get("uTime", 0))
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if timestamp > 0:
                        connection.execute(
                            "UPDATE orders SET exchange_updated_ms = ? WHERE client_order_id = ?",
                            (timestamp, row["client_order_id"]),
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

    @staticmethod
    def _write_order(
        connection: sqlite3.Connection,
        order: dict[str, Any],
        *,
        replace: bool,
    ) -> sqlite3.Cursor:
        now = order.get("updated_at") or _utc_now()
        created_at = order.get("created_at") or now
        on_conflict = (
            """
            DO UPDATE SET
                exchange_order_id = excluded.exchange_order_id,
                status = excluded.status,
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at,
                exchange_updated_ms = COALESCE(excluded.exchange_updated_ms, orders.exchange_updated_ms),
                account_scope = COALESCE(orders.account_scope, excluded.account_scope),
                order_kind = excluded.order_kind
            """ if replace else "DO NOTHING"
        )
        return connection.execute(
                """
                INSERT INTO orders (
                    client_order_id, exchange_order_id, status, inst_id, side,
                    pos_side, ord_type, td_mode, size, price, reduce_only,
                    stop_loss, take_profit, source, raw_json, created_at, updated_at,
                    risk_notional, exchange_updated_ms, account_scope, order_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id)
                """ + on_conflict,
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
                    order.get("risk_notional"),
                    order.get("exchange_updated_ms"),
                    order.get("account_scope"),
                    order.get("order_kind", "standard"),
                ),
            )

    def save_order(self, order: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (order["client_order_id"],),
            ).fetchone()
            if previous and previous["status"] in TERMINAL_ORDER_STATUSES and previous["status"] != order["status"]:
                return dict(previous)
            self._write_order(connection, order, replace=True)
        return self.get_order(order["client_order_id"]) or order

    def save_exchange_order(self, order: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Apply monotonic exchange snapshots atomically across REST and WS writers."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (order["client_order_id"],),
            ).fetchone()
            previous = dict(row) if row else None
            if previous:
                for field in ("inst_id", "exchange_order_id", "account_scope", "order_kind"):
                    if previous.get(field) and order.get(field) and previous[field] != order[field]:
                        raise OrderSnapshotConflict(f"order_{field}_mismatch")
                old_status, new_status = previous["status"], order["status"]
                old_time, new_time = previous["exchange_updated_ms"], order.get("exchange_updated_ms")
                if old_time is not None and (new_time is None or new_time < old_time):
                    return previous, False
                if old_status in TERMINAL_ORDER_STATUSES and new_status != old_status:
                    return previous, False
                if old_status == "partially_filled" and new_status in {"unknown", "live", "pending", "submitted"}:
                    return previous, False
                if old_time == new_time and old_status == new_status and previous["raw_json"] == json.dumps(order.get("raw", {}), ensure_ascii=True):
                    return previous, False
            self._write_order(connection, order, replace=True)
            if previous and previous["source"].startswith("okx-"):
                connection.execute(
                    """UPDATE orders SET side = ?, pos_side = ?, ord_type = ?, td_mode = ?,
                       size = ?, price = ?, reduce_only = ?, stop_loss = ?, take_profit = ?
                       WHERE client_order_id = ?""",
                    (order["side"], order["pos_side"], order["ord_type"], order["td_mode"],
                     order["size"], order.get("price"), int(bool(order.get("reduce_only"))),
                     order.get("stop_loss"), order.get("take_profit"), order["client_order_id"]),
                )
            connection.execute("UPDATE execution_generation SET generation = generation + 1 WHERE singleton = 1")
            result = connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (order["client_order_id"],),
            ).fetchone()
            return dict(result), True

    def execution_snapshot(self) -> tuple[int, list[dict[str, Any]]]:
        with self._connection() as connection:
            connection.execute("BEGIN")
            generation = connection.execute(
                "SELECT generation FROM execution_generation WHERE singleton = 1",
            ).fetchone()[0]
            placeholders = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
            rows = connection.execute(
                f"SELECT * FROM orders WHERE status IN ({placeholders})",
                ACTIVE_ORDER_STATUSES,
            ).fetchall()
        return int(generation), [dict(row) for row in rows]

    def claim_order(
        self,
        order: dict[str, Any],
        *,
        expected_generation: int | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Commit an intent before network I/O; one claimant wins across processes."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?",
                (order["client_order_id"],),
            ).fetchone()
            if existing:
                return False, dict(existing)
            if expected_generation is not None:
                current = connection.execute(
                    "SELECT generation FROM execution_generation WHERE singleton = 1",
                ).fetchone()[0]
                if current != expected_generation:
                    raise ExposureSnapshotChanged("execution_budget_snapshot_changed")
            inserted = self._write_order(connection, order, replace=False).rowcount == 1
            if inserted and order["status"] != "preview":
                connection.execute(
                    "UPDATE execution_generation SET generation = generation + 1 WHERE singleton = 1",
                )
            row = connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?",
                (order["client_order_id"],),
            ).fetchone()
        return inserted, dict(row)

    def finalize_submission(
        self,
        client_order_id: str,
        status: str,
        *,
        raw: dict[str, Any],
        exchange_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Do not overwrite a private-stream update that raced the HTTP response."""
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE orders SET status = ?, raw_json = ?,
                    exchange_order_id = COALESCE(?, exchange_order_id), updated_at = ?
                WHERE client_order_id = ? AND status IN ('preparing', 'submitting', 'submission_unknown')
                """,
                (status, json.dumps(raw, ensure_ascii=True), exchange_order_id,
                 _utc_now(), client_order_id),
            )
        return self.get_order(client_order_id) or {}

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
        active_statuses = ACTIVE_ORDER_STATUSES
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

    def save_bill_snapshot(
        self,
        account_scope: str,
        bills: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT captured_at FROM account_bill_snapshots WHERE account_scope = ?",
                (account_scope,),
            ).fetchone()
            if previous and previous["captured_at"] > summary["captured_at"]:
                return
            for bill in bills:
                connection.execute(
                    """
                    INSERT INTO account_bills
                        (account_scope, bill_id, timestamp_ms, currency, inst_id, kind, record_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_scope, bill_id) DO UPDATE SET
                        timestamp_ms = excluded.timestamp_ms,
                        currency = excluded.currency, inst_id = excluded.inst_id,
                        kind = excluded.kind, record_json = excluded.record_json
                    """,
                    (
                        account_scope, bill["bill_id"], bill["timestamp_ms"], bill["currency"],
                        bill["inst_id"], bill["kind"], json.dumps(bill, ensure_ascii=True),
                    ),
                )
            connection.execute(
                """
                INSERT INTO account_bill_snapshots (account_scope, captured_at, summary_json)
                VALUES (?, ?, ?)
                ON CONFLICT(account_scope) DO UPDATE SET
                    captured_at = excluded.captured_at, summary_json = excluded.summary_json
                """,
                (account_scope, summary["captured_at"], json.dumps(summary, ensure_ascii=True)),
            )

    def bill_snapshot(self, account_scope: str, limit: int = 100) -> dict[str, Any]:
        with self._connection() as connection:
            snapshot = connection.execute(
                "SELECT summary_json FROM account_bill_snapshots WHERE account_scope = ?",
                (account_scope,),
            ).fetchone()
            rows = connection.execute(
                """SELECT record_json FROM account_bills WHERE account_scope = ?
                   ORDER BY timestamp_ms DESC, bill_id DESC LIMIT ?""",
                (account_scope, max(1, min(limit, 500)) + 1),
            ).fetchall()
        selected_limit = max(1, min(limit, 500))
        summary = json.loads(snapshot["summary_json"]) if snapshot else None
        now = datetime.now(timezone.utc)
        age = (now - datetime.fromisoformat(summary["captured_at"])).total_seconds() if summary else None
        return {
            "data": [json.loads(row["record_json"]) for row in rows[:selected_limit]],
            "has_more": len(rows) > selected_limit,
            "summary": summary,
            "fresh": bool(summary and summary["day_utc"] == now.date().isoformat() and -5 <= age <= 45),
        }

    @staticmethod
    def _expire_bill_imports(connection: sqlite3.Connection, scope: str, now_ms: int) -> None:
        connection.execute(
            """UPDATE account_bill_imports SET status = 'interrupted',
               finished_at = ?, error = 'import_lease_expired'
               WHERE account_scope = ? AND status = 'running' AND lease_until_ms <= ?""",
            (datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(), scope, now_ms),
        )

    @staticmethod
    def _bill_import(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {key: row[key] for key in row.keys() if key not in {"account_scope", "lease_until_ms"}}

    def create_bill_import(self, scope: str, days: list[date], now_ms: int) -> dict[str, Any]:
        job_id = uuid4().hex
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_bill_imports(connection, scope, now_ms)
            if connection.execute(
                "SELECT 1 FROM account_bill_imports WHERE account_scope = ? AND status = 'running'",
                (scope,),
            ).fetchone():
                raise BillImportBusy("history_import_already_running")
            connection.execute(
                """INSERT INTO account_bill_imports
                   (id, account_scope, start_day, end_day, status, total_days, started_at, lease_until_ms)
                   VALUES (?, ?, ?, ?, 'running', ?, ?, ?)""",
                (job_id, scope, days[0].isoformat(), days[-1].isoformat(), len(days),
                 datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(), now_ms + 120_000),
            )
            return self._bill_import(connection.execute(
                "SELECT * FROM account_bill_imports WHERE id = ?", (job_id,),
            ).fetchone())

    def touch_bill_import(self, job_id: str, scope: str, now_ms: int) -> None:
        with self._connection() as connection:
            updated = connection.execute(
                """UPDATE account_bill_imports SET lease_until_ms = ?
                   WHERE id = ? AND account_scope = ? AND status = 'running' AND lease_until_ms > ?""",
                (now_ms + 120_000, job_id, scope, now_ms),
            ).rowcount
            if not updated:
                raise BillImportLeaseLost("history_import_lease_lost")

    def finish_bill_import(self, job_id: str, scope: str, status: str, error: str | None = None) -> None:
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError("invalid_import_terminal_status")
        with self._connection() as connection:
            connection.execute(
                """UPDATE account_bill_imports SET status = ?, error = ?, finished_at = ?
                   WHERE id = ? AND account_scope = ? AND status = 'running'
                   AND (? != 'completed' OR completed_days = total_days)""",
                (status, error, _utc_now(), job_id, scope, status),
            )

    def commit_bill_history_window(
        self, job_id: str, scope: str, day: date,
        records: list[dict[str, Any]], summary: dict[str, Any], now_ms: int,
        *, archive_lease: tuple[str, str] | None = None,
    ) -> None:
        begin, end = day_ms(day), day_ms(day) + DAY_MS
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if archive_lease is not None:
                self._require_archive_lease(connection, archive_lease[0], scope, archive_lease[1], now_ms)
            job = connection.execute(
                """SELECT * FROM account_bill_imports WHERE id = ? AND account_scope = ?
                   AND status = 'running' AND lease_until_ms > ?""",
                (job_id, scope, now_ms),
            ).fetchone()
            if job is None:
                raise BillImportLeaseLost("history_import_lease_lost")
            expected = date.fromisoformat(job["start_day"]).toordinal() + job["completed_days"]
            if day.toordinal() != expected or day.isoformat() > job["end_day"]:
                raise ValueError("history_window_out_of_order")
            for record in records:
                if not begin <= record["timestamp_ms"] < end:
                    raise ValueError("history_bill_outside_window")
                existing = connection.execute(
                    "SELECT timestamp_ms FROM account_bill_history WHERE account_scope = ? AND bill_id = ?",
                    (scope, record["bill_id"]),
                ).fetchone()
                if existing and existing["timestamp_ms"] != record["timestamp_ms"]:
                    raise ValueError("history_bill_identity_conflict")
            connection.execute(
                "DELETE FROM account_bill_history WHERE account_scope = ? AND timestamp_ms >= ? AND timestamp_ms < ?",
                (scope, begin, end),
            )
            connection.executemany(
                "INSERT INTO account_bill_history (account_scope, bill_id, timestamp_ms, record_json) VALUES (?, ?, ?, ?)",
                [(scope, record["bill_id"], record["timestamp_ms"], json.dumps(record, ensure_ascii=True, allow_nan=False))
                 for record in records],
            )
            connection.execute(
                """INSERT INTO account_bill_history_windows (account_scope, day_utc, captured_at, job_id, summary_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(account_scope, day_utc) DO UPDATE SET
                       captured_at = excluded.captured_at, job_id = excluded.job_id, summary_json = excluded.summary_json""",
                (scope, day.isoformat(), _utc_now(), job_id, json.dumps(summary, ensure_ascii=True)),
            )
            connection.execute(
                """UPDATE account_bill_imports SET completed_days = completed_days + 1,
                   rows_imported = rows_imported + ?, lease_until_ms = ? WHERE id = ?""",
                (len(records), now_ms + 120_000, job_id),
            )

    def bill_import(self, scope: str, job_id: str | None = None) -> dict[str, Any] | None:
        with self._connection() as connection:
            self._expire_bill_imports(connection, scope, int(datetime.now(timezone.utc).timestamp() * 1000))
            if job_id:
                row = connection.execute(
                    "SELECT * FROM account_bill_imports WHERE account_scope = ? AND id = ?", (scope, job_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM account_bill_imports WHERE account_scope = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                    (scope,),
                ).fetchone()
            return self._bill_import(row)

    @staticmethod
    def _bill_archive(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {key: row[key] for key in row.keys() if key not in {
            "account_scope", "lease_owner", "lease_until_ms",
        }}

    def create_bill_archive(
        self, scope: str, year: int, quarter: str, now_ms: int, *, retry: bool = False,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM account_bill_archives WHERE account_scope = ? AND year = ? AND quarter = ?",
                (scope, year, quarter),
            ).fetchone()
            if row:
                if retry and row["state"] in {"completed", "failed", "canceled"}:
                    connection.execute(
                        """UPDATE account_bill_archives SET state = 'queued', requested_at_ms = NULL,
                           next_attempt_ms = ?, lease_owner = NULL, lease_until_ms = 0,
                           import_job_id = NULL, failures = 0, error = NULL, updated_at = ? WHERE id = ?""",
                        (now_ms, _utc_now(), row["id"]),
                    )
                job_id = row["id"]
            else:
                job_id = uuid4().hex
                connection.execute(
                    """INSERT INTO account_bill_archives
                       (id, account_scope, year, quarter, state, next_attempt_ms, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)""",
                    (job_id, scope, year, quarter, now_ms, _utc_now(), _utc_now()),
                )
            return self._bill_archive(connection.execute(
                "SELECT * FROM account_bill_archives WHERE id = ?", (job_id,),
            ).fetchone())

    def bill_archives(self, scope: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT a.*, i.completed_days, i.total_days, i.rows_imported
                   FROM account_bill_archives a LEFT JOIN account_bill_imports i ON i.id = a.import_job_id
                   WHERE a.account_scope = ? ORDER BY a.year DESC, a.quarter DESC LIMIT 100""",
                (scope,),
            ).fetchall()
            return [self._bill_archive(row) for row in rows]

    @staticmethod
    def _require_archive_lease(connection, job_id: str, scope: str, owner: str, now_ms: int):
        row = connection.execute(
            """SELECT * FROM account_bill_archives WHERE id = ? AND account_scope = ?
               AND lease_owner = ? AND lease_until_ms > ?
               AND state NOT IN ('completed', 'failed', 'canceled')""",
            (job_id, scope, owner, now_ms),
        ).fetchone()
        if row is None:
            raise BillImportLeaseLost("archive_lease_lost")
        return row

    def claim_bill_archive(self, scope: str, owner: str, now_ms: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM account_bill_archives WHERE account_scope = ?
                   AND state NOT IN ('completed', 'failed', 'canceled')
                   AND next_attempt_ms <= ? AND lease_until_ms <= ?
                   ORDER BY next_attempt_ms, created_at LIMIT 1""",
                (scope, now_ms, now_ms),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE account_bill_archives SET lease_owner = ?, lease_until_ms = ? WHERE id = ?",
                (owner, now_ms + 120_000, row["id"]),
            )
            return self._bill_archive(row)

    def update_bill_archive(
        self, job_id: str, scope: str, owner: str, now_ms: int, *,
        state: str, next_attempt_ms: int | None = None, error: str | None = None,
        requested_at_ms: int | None = None, import_job_id: str | None = None, failures: int = 0,
        release: bool = False,
    ) -> None:
        if state not in {"queued", "requesting", "waiting", "downloading", "importing", "completed", "failed"}:
            raise ValueError("archive_state_invalid")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_archive_lease(connection, job_id, scope, owner, now_ms)
            linked = import_job_id or current["import_job_id"]
            if state == "completed":
                complete = connection.execute(
                    """SELECT 1 FROM account_bill_imports WHERE id = ? AND account_scope = ?
                       AND status = 'completed' AND completed_days = total_days""", (linked, scope),
                ).fetchone()
                if complete is None:
                    raise ValueError("archive_import_incomplete")
            connection.execute(
                """UPDATE account_bill_archives SET state = ?, next_attempt_ms = ?, error = ?,
                   requested_at_ms = ?, import_job_id = ?, updated_at = ?, failures = ?,
                   lease_owner = ?, lease_until_ms = ? WHERE id = ?""",
                (state, now_ms if next_attempt_ms is None else next_attempt_ms, error,
                 requested_at_ms if requested_at_ms is not None else current["requested_at_ms"],
                 linked, _utc_now(), failures,
                 None if release else owner, 0 if release else now_ms + 120_000, job_id),
            )

    def touch_bill_archive(self, job_id: str, scope: str, owner: str, now_ms: int) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_archive_lease(connection, job_id, scope, owner, now_ms)
            connection.execute(
                "UPDATE account_bill_archives SET lease_until_ms = ? WHERE id = ?",
                (now_ms + 120_000, job_id),
            )

    def cancel_bill_archive(self, job_id: str, scope: str) -> bool:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM account_bill_archives WHERE id = ? AND account_scope = ?", (job_id, scope),
            ).fetchone()
            if row is None:
                return False
            if row["state"] not in {"completed", "failed", "canceled"}:
                connection.execute(
                    """UPDATE account_bill_archives SET state = 'canceled', lease_owner = NULL,
                       lease_until_ms = 0, updated_at = ? WHERE id = ?""", (_utc_now(), job_id),
                )
                connection.execute(
                    """UPDATE account_bill_imports SET status = 'interrupted', error = 'archive_canceled',
                       finished_at = ? WHERE id = ? AND status = 'running'""", (_utc_now(), row["import_job_id"]),
                )
            return True

    def bill_history(
        self, scope: str, days: list[date], *, limit: int = 100,
        before_ts: int | None = None, before_id: str | None = None,
    ) -> dict[str, Any]:
        begin, end = day_ms(days[0]), day_ms(days[-1]) + DAY_MS
        clause = "account_scope = ? AND timestamp_ms >= ? AND timestamp_ms < ?"
        params: list[Any] = [scope, begin, end]
        if (before_ts is None) != (before_id is None):
            raise ValueError("history_cursor_pair_required")
        cursor_clause = "" if before_ts is None else " AND (timestamp_ms < ? OR (timestamp_ms = ? AND bill_id < ?))"
        cursor_params = [] if before_ts is None else [before_ts, before_ts, before_id]
        limit = max(1, min(limit, 500))
        with self._connection() as connection:
            # Read rows, counts and coverage from one SQLite snapshot.
            connection.execute("BEGIN")
            rows = connection.execute(
                f"SELECT record_json FROM account_bill_history WHERE {clause}{cursor_clause} ORDER BY timestamp_ms DESC, bill_id DESC LIMIT ?",
                [*params, *cursor_params, limit + 1],
            ).fetchall()
            total = connection.execute(f"SELECT COUNT(*) FROM account_bill_history WHERE {clause}", params).fetchone()[0]
            windows = connection.execute(
                """SELECT day_utc, captured_at, summary_json FROM account_bill_history_windows
                   WHERE account_scope = ? AND day_utc >= ? AND day_utc <= ? ORDER BY day_utc""",
                (scope, days[0].isoformat(), days[-1].isoformat()),
            ).fetchall()
        data = [json.loads(row["record_json"]) for row in rows[:limit]]
        covered = {row["day_utc"] for row in windows}
        missing = [day.isoformat() for day in days if day.isoformat() not in covered]
        return {
            "data": data, "total": total,
            "next_cursor": {"timestamp_ms": data[-1]["timestamp_ms"], "bill_id": data[-1]["bill_id"]} if len(rows) > limit else None,
            "coverage": {
                "start_day": days[0].isoformat(), "end_day": days[-1].isoformat(),
                "complete": not missing, "completed_days": len(covered), "total_days": len(days),
                "missing_days": missing, "last_imported_at": max((row["captured_at"] for row in windows), default=None),
            },
            "summary": combine_history_windows([json.loads(row["summary_json"]) for row in windows]),
        }

    def pnl_summary(self, limit: int = 500) -> dict[str, Any]:
        fills = self.list_fills(limit)
        currency, valuation = self._fill_report_currency(fills)
        if valuation not in {"single_currency", "empty"}:
            return {
                "fills": len(fills), "currency": None, "valuation_status": valuation,
                "basis": "fills_only_excludes_funding",
                "realized_pnl": None, "fees": None, "net_pnl": None, "by_instrument": {},
            }
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
            "currency": currency,
            "valuation_status": valuation,
            "basis": "fills_only_excludes_funding",
            "realized_pnl": round(realized_pnl, 8),
            "fees": round(fees, 8),
            "net_pnl": round(realized_pnl + fees, 8),
            "by_instrument": by_instrument,
        }

    @staticmethod
    def _fill_report_currency(fills: list[dict[str, Any]]) -> tuple[str | None, str]:
        currencies: set[str] = set()
        for fill in fills:
            parts = str(fill.get("inst_id") or "").split("-")
            if len(parts) != 3 or parts[-1] != "SWAP" or parts[1] not in {"USD", "USDT", "USDC"}:
                return None, "unresolved_currency"
            settlement = parts[0] if parts[1] == "USD" else parts[1]
            currencies.add(settlement)
            try:
                pnl, fee = float(fill.get("realized_pnl") or 0), float(fill.get("fee") or 0)
            except (ValueError, TypeError):
                return None, "invalid_amount"
            if not math.isfinite(pnl) or not math.isfinite(fee):
                return None, "invalid_amount"
            if fee:
                if not fill.get("fee_ccy"):
                    return None, "unresolved_currency"
                currencies.add(str(fill["fee_ccy"]))
        if len(currencies) > 1:
            return None, "mixed_currency"
        return next(iter(currencies), None), "single_currency" if currencies else "empty"

    def performance_report(
        self,
        *,
        initial_equity: float = 1000.0,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Build realized performance metrics from the durable fill ledger."""
        if not math.isfinite(initial_equity) or initial_equity <= 0:
            raise ValueError("initial_equity must be finite and greater than zero")
        fills = sorted(
            self.list_fills(limit),
            key=lambda item: str(item.get("filled_at") or ""),
        )
        currency, valuation = self._fill_report_currency(fills)
        if valuation not in {"single_currency", "empty"}:
            return {
                "fills": len(fills), "currency": None, "valuation_status": valuation,
                "basis": "fills_only_excludes_funding",
                "initial_equity": initial_equity, "ending_equity": None,
                "realized_pnl": None, "fees": None, "net_pnl": None, "return_pct": None,
                "max_drawdown": None, "max_drawdown_pct": None,
                "daily": {}, "by_strategy": {}, "equity_curve": [],
            }
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
            "currency": currency,
            "valuation_status": valuation,
            "basis": "fills_only_excludes_funding",
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

    def analysis_index(
        self,
        limit: int = 20,
        *,
        before_id: int | None = None,
        inst_id: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value, operator in (
            ("id", before_id, "<"),
            ("inst_id", inst_id, "="),
            ("source", source, "="),
        ):
            if value is not None:
                clauses.append(f"{column} {operator} ?")
                parameters.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        page_size = max(1, min(limit, 50))
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT id, inst_id, source, bias, created_at FROM analyses
                    {where} ORDER BY id DESC LIMIT ?""",
                (*parameters, page_size + 1),
            ).fetchall()
        page = [dict(row) for row in rows[:page_size]]
        return {
            "data": page,
            "next_before_id": page[-1]["id"] if len(rows) > page_size else None,
        }

    def get_analysis(self, analysis_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM analyses WHERE id = ?", (analysis_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["signal"] = json.loads(item.pop("signal_json"))
        item["report"] = json.loads(item.pop("report_json"))
        return item

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
