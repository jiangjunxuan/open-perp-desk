"""Offline recovery and online snapshots without starting API integrations."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class MaintenanceError(RuntimeError):
    pass


REQUIRED_COLUMNS = {
    "orders": {"client_order_id", "status", "inst_id", "size", "raw_json"},
    "positions": {"position_key", "inst_id", "size", "status"},
    "fills": {"trade_id", "inst_id", "fill_price", "fill_size"},
    "strategies": {"strategy_id", "enabled", "config_json", "updated_at"},
    "control_flags": {"name", "value", "reason", "updated_at"},
    "audit_events": {"event_type", "severity", "message", "payload_json", "created_at"},
}
RESTORE_ENVIRONMENT = {
    "EXECUTION_ENABLED": "false",
    "LIVE_TRADING_ENABLED": "false",
    "AUTO_TRADING_ENABLED": "false",
    "AUTO_TRADING_DRY_RUN": "true",
}


def configured_database() -> Path:
    return Path(os.getenv("STATE_DB_PATH") or Path(os.getenv("DATA_DIR", "./data")) / "openperpdesk.sqlite3").absolute()


def recovery_marker(path: Path) -> Path:
    return path.with_name(f".{path.name}.restore-in-progress")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_database(path: Path) -> None:
    if not path.is_file():
        raise MaintenanceError("Database file does not exist.")
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise MaintenanceError("File is not a SQLite database.")
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise MaintenanceError("SQLite integrity check failed.")
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger', 'view')").fetchone():
                raise MaintenanceError("Unexpected executable database schema.")
            for table, expected in REQUIRED_COLUMNS.items():
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not expected <= columns:
                    raise MaintenanceError(f"Unsupported OpenPerpDesk schema: {table}.")
    except sqlite3.Error as error:
        raise MaintenanceError("SQLite validation failed.") from error


def online_snapshot(source: Path, destination: Path, timeout: float = 60) -> None:
    if recovery_marker(source).exists():
        raise MaintenanceError("Database recovery is incomplete; snapshot refused.")
    validate_database(source)
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    deadline = time.monotonic() + timeout

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise MaintenanceError("Snapshot timed out under database contention.")

    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as origin:
            with closing(sqlite3.connect(destination)) as snapshot:
                origin.backup(snapshot, pages=256, progress=progress, sleep=.05)
                snapshot.execute("PRAGMA journal_mode=DELETE")
        validate_database(destination)
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
        fsync_directory(destination.parent)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def assert_restore_environment(environment: dict | None = None) -> None:
    environment = os.environ if environment is None else environment
    for name, expected in RESTORE_ENVIRONMENT.items():
        if str(environment.get(name, "")).lower() != expected:
            raise MaintenanceError(f"Recovery requires {name}={expected}.")


def write_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
            json.dump(payload, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def archive_database(target: Path, archive: Path) -> None:
    archive.mkdir(mode=0o700)
    raw = archive / "raw"
    raw.mkdir(mode=0o700)
    files = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        source = Path(str(target) + suffix)
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_file():
            raise MaintenanceError("Refusing a non-regular database or sidecar.")
        destination = raw / source.name
        with source.open("rb") as origin, destination.open("xb") as output:
            os.chmod(destination, 0o600)
            shutil.copyfileobj(origin, output)
            output.flush()
            os.fsync(output.fileno())
        files.append({"name": source.name, "sha256": file_digest(destination)})
    fsync_directory(raw)
    snapshot_created = False
    # Normalize a valid prior WAL file set to a single-file rollback snapshot.
    if files and (raw / target.name).exists():
        try:
            online_snapshot(target, archive / "rollback.sqlite3")
            snapshot_created = True
        except (MaintenanceError, sqlite3.Error):
            pass  # A damaged prior database is still preserved verbatim.
    write_json(archive / "manifest.json", {
        "format": 1, "database": target.name, "files": files,
        "snapshot": "rollback.sqlite3" if snapshot_created else None,
    })
    fsync_directory(archive.parent)


def restore_database(source: Path, target: Path, *, offline: bool = False) -> dict:
    assert_restore_environment()
    if not offline:
        raise MaintenanceError("Restore requires the API to be stopped.")
    validate_database(source)
    if source.resolve() == target.resolve() or target.is_symlink():
        raise MaintenanceError("Invalid recovery source or target.")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f".{target.name}.maintenance.lock")
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MaintenanceError("Another database maintenance operation is active.") from error
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        recovery_dir = target.parent / "recovery"
        recovery_dir.mkdir(mode=0o700, exist_ok=True)
        archive = recovery_dir / f"before-{stamp}-{uuid4().hex[:10]}"
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=target.parent) as directory:
            candidate = Path(directory) / "candidate.sqlite3"
            online_snapshot(source, candidate)
            archive_database(target, archive)
            now = datetime.now(timezone.utc).isoformat()
            with closing(sqlite3.connect(candidate)) as connection:
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute(
                    "INSERT INTO control_flags(name, value, reason, updated_at) VALUES('emergency_stop', 'true', ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET value='true', reason=excluded.reason, updated_at=excluded.updated_at",
                    ("Database restored; reconcile account and orders before manual resume.", now),
                )
                connection.execute("UPDATE strategies SET enabled=0, updated_at=?", (now,))
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tradingview_alerts'"
                ).fetchone():
                    # A saved queued alert may have executed after the backup
                    # was taken. Restoring it must not authorize another send.
                    connection.execute(
                        """UPDATE tradingview_alerts SET status='interrupted', updated_at=?,
                            result_json=?, owner=NULL WHERE status IN ('queued', 'processing')""",
                        (now, json.dumps({"reasons": ["restored_inbox_requires_review"]})),
                    )
                connection.execute(
                    "INSERT INTO audit_events(event_type, severity, message, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
                    ("database_restored", "warning", "Database restored with execution stopped.",
                     json.dumps({"archive": str(archive), "source_sha256": file_digest(source)}), now),
                )
                connection.commit()
            validate_database(candidate)
            with candidate.open("rb") as stream:
                os.fsync(stream.fileno())
            marker = recovery_marker(target)
            write_json(marker, {"archive": str(archive), "started_at": now})
            # The durable marker prevents API startup if recovery is interrupted here.
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(target) + suffix).unlink(missing_ok=True)
            os.replace(candidate, target)
            os.chmod(target, 0o600)
            fsync_directory(target.parent)
            validate_database(target)
            marker.unlink()
            fsync_directory(target.parent)
        return {"restored": True, "database": str(target), "archive": str(archive), "emergency_stopped": True}


def verify_runtime_status(status: dict) -> None:
    worker = status.get("automation_worker", {})
    if not (
        status.get("execution_enabled") is False
        and worker.get("enabled") is False
        and worker.get("running") is False
        and worker.get("dry_run") is True
        and status.get("live_safety", {}).get("configuration_enabled") is False
        and status.get("live_safety", {}).get("allowed") is False
        and status.get("safety_control", {}).get("emergency_stopped") is True
    ):
        raise MaintenanceError("Restored API did not confirm all execution locks.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("info", "backup", "restore", "verify-runtime"))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--sha256")
    args = parser.parse_args()
    path = configured_database()
    if args.command == "info":
        print(json.dumps({"format": 1, "database": str(path)}))
    elif args.command == "verify-runtime":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:8000/api/v1/system/status", timeout=10) as response:
            verify_runtime_status(json.load(response))
        print(json.dumps({"execution_locked": True, "emergency_stopped": True}))
    elif args.command == "backup":
        with tempfile.TemporaryDirectory(prefix=".backup-", dir=path.parent) as directory:
            snapshot = Path(directory) / "snapshot.sqlite3"
            online_snapshot(path, snapshot)
            with snapshot.open("rb") as stream:
                shutil.copyfileobj(stream, sys.stdout.buffer)
    else:
        assert_restore_environment()
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".incoming-", dir=path.parent) as directory:
            incoming = Path(directory) / "incoming.sqlite3"
            with incoming.open("xb") as stream:
                os.chmod(incoming, 0o600)
                shutil.copyfileobj(sys.stdin.buffer, stream)
            if not args.sha256 or file_digest(incoming) != args.sha256:
                raise MaintenanceError("Recovery transfer checksum mismatch.")
            print(json.dumps(restore_database(incoming, path, offline=args.offline)))


if __name__ == "__main__":
    try:
        main()
    except (MaintenanceError, OSError, sqlite3.Error, ValueError) as error:
        print(f"Database maintenance failed: {error}", file=sys.stderr)
        raise SystemExit(1)
