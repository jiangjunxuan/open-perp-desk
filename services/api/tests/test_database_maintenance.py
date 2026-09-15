import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import database_maintenance as maintenance
from app.safety_control import SafetyController
from app.state_store import StateStore


class DatabaseMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source.sqlite3"
        self.store = StateStore(str(self.source))
        self.environment = patch.dict(os.environ, maintenance.RESTORE_ENVIRONMENT)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_configured_path_uses_override_then_data_directory(self):
        with patch.dict(os.environ, {"STATE_DB_PATH": str(self.root / "nested" / "custom.db"), "DATA_DIR": "/ignored"}):
            self.assertEqual(maintenance.configured_database(), self.root / "nested" / "custom.db")
        with patch.dict(os.environ, {"STATE_DB_PATH": "", "DATA_DIR": str(self.root)}):
            self.assertEqual(maintenance.configured_database(), self.root / "openperpdesk.sqlite3")

    def test_rejects_invalid_unrelated_and_missing_databases(self):
        invalid = self.root / "invalid.sqlite3"
        invalid.write_bytes(b"not sqlite")
        unrelated = self.root / "unrelated.sqlite3"
        with closing(sqlite3.connect(unrelated)) as connection:
            connection.execute("CREATE TABLE other(id INTEGER)")
        for path in (invalid, unrelated, self.root / "missing.sqlite3"):
            with self.subTest(path=path), self.assertRaises(maintenance.MaintenanceError):
                maintenance.validate_database(path)
        self.assertFalse((self.root / "missing.sqlite3").exists())

    def test_rejects_unexpected_trigger(self):
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute("CREATE TRIGGER unexpected AFTER UPDATE ON strategies BEGIN DELETE FROM orders; END")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "executable"):
            maintenance.validate_database(self.source)

    def test_snapshot_contains_uncheckpointed_wal(self):
        destination = self.root / "backup.sqlite3"
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("UPDATE strategies SET enabled=1")
            connection.commit()
            self.assertGreater(Path(str(self.source) + "-wal").stat().st_size, 0)
            maintenance.online_snapshot(self.source, destination)
        with closing(sqlite3.connect(destination)) as connection:
            self.assertEqual(connection.execute("SELECT enabled FROM strategies").fetchone(), (1,))
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone(), ("delete",))
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertFalse(Path(str(destination) + "-wal").exists())

    def test_snapshot_is_consistent_during_concurrent_writes(self):
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE pairs(id INTEGER PRIMARY KEY, side INTEGER)")
            connection.commit()
        started, stopped = threading.Event(), threading.Event()
        failures = []

        def writer():
            try:
                with closing(sqlite3.connect(self.source)) as connection:
                    for index in range(200):
                        with connection:
                            connection.executemany("INSERT INTO pairs VALUES(?, ?)", [(index * 2, 0), (index * 2 + 1, 1)])
                        started.set()
                        if stopped.wait(.001):
                            break
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            destination = self.root / "concurrent.sqlite3"
            maintenance.online_snapshot(self.source, destination)
            with closing(sqlite3.connect(destination)) as connection:
                count, total = connection.execute("SELECT count(*), sum(side) FROM pairs").fetchone()
                self.assertGreater(count, 0)
                self.assertEqual(count, total * 2)
        finally:
            stopped.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures)

    def test_snapshot_never_overwrites_destination(self):
        destination = self.root / "existing.sqlite3"
        destination.write_bytes(b"preserve")
        with self.assertRaises(FileExistsError):
            maintenance.online_snapshot(self.source, destination)
        self.assertEqual(destination.read_bytes(), b"preserve")

    def test_failed_snapshot_removes_partial_output(self):
        destination = self.root / "partial.sqlite3"
        with self.assertRaisesRegex(maintenance.MaintenanceError, "timed out"):
            maintenance.online_snapshot(self.source, destination, timeout=-1)
        self.assertFalse(destination.exists())

    def test_restore_is_offline_and_requires_all_safe_flags(self):
        target = self.root / "target.sqlite3"
        with self.assertRaisesRegex(maintenance.MaintenanceError, "stopped"):
            maintenance.restore_database(self.source, target)
        for name, expected in maintenance.RESTORE_ENVIRONMENT.items():
            with self.subTest(name=name), patch.dict(os.environ, {name: "false" if expected == "true" else "true"}):
                with self.assertRaises(maintenance.MaintenanceError):
                    maintenance.restore_database(self.source, target, offline=True)
        self.assertFalse(target.exists())

    def test_restore_preserves_previous_database_and_locks_new_one(self):
        target = self.root / "nested" / "custom.db"
        old = StateStore(str(target))
        old.set_control_flag("old_marker", True, "old value")
        self.store.set_control_flag("backup_marker", True, "backup value")
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute("UPDATE strategies SET enabled=1")
            connection.commit()
        result = maintenance.restore_database(self.source, target, offline=True)
        restored = StateStore(str(target))
        self.assertTrue(restored.get_control_flag("backup_marker"))
        self.assertFalse(restored.get_control_flag("old_marker"))
        self.assertTrue(SafetyController(restored).emergency_stopped)
        with closing(sqlite3.connect(target)) as connection:
            self.assertEqual(connection.execute("SELECT enabled FROM strategies").fetchone(), (0,))
            self.assertEqual(connection.execute("SELECT count(*) FROM audit_events WHERE event_type='database_restored'").fetchone(), (1,))
        rollback = StateStore(str(Path(result["archive"]) / "rollback.sqlite3"))
        self.assertTrue(rollback.get_control_flag("old_marker"))
        self.assertFalse(SafetyController(rollback).emergency_stopped)
        manifest = json.loads((Path(result["archive"]) / "manifest.json").read_text())
        for item in manifest["files"]:
            self.assertEqual(maintenance.file_digest(Path(result["archive"]) / "raw" / item["name"]), item["sha256"])
        self.assertFalse(maintenance.recovery_marker(target).exists())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertFalse(SafetyController(self.store).emergency_stopped)

    def test_restore_replaces_corrupt_target_but_keeps_raw_evidence(self):
        target = self.root / "damaged.sqlite3"
        target.write_bytes(b"damaged database")
        result = maintenance.restore_database(self.source, target, offline=True)
        archive = Path(result["archive"])
        self.assertEqual((archive / "raw" / target.name).read_bytes(), b"damaged database")
        self.assertIsNone(json.loads((archive / "manifest.json").read_text())["snapshot"])
        maintenance.validate_database(target)

    def test_restore_never_replays_queued_or_processing_tradingview_alerts(self):
        instruction = {
            "signal": {"inst_id": "BTC-USDT-SWAP", "action": "open_long"},
            "dry_run": False, "client_order_id": "opdBackupFixture",
        }
        for identifier in ("completed", "processing", "queued"):
            self.store.enqueue_tradingview_alert(identifier, identifier, instruction)
        self.store.claim_tradingview_alert("old-worker", 1)
        self.store.finish_tradingview_alert("completed", "old-worker", "submitted", {"accepted": True})
        self.store.claim_tradingview_alert("old-worker", 1)
        restored_path = self.root / "restored.sqlite3"
        maintenance.restore_database(self.source, restored_path, offline=True)
        restored = StateStore(str(restored_path))
        rows = {row["alert_id"]: row for row in restored.list_tradingview_alerts()}
        self.assertEqual(rows["completed"]["status"], "submitted")
        for identifier in ("processing", "queued"):
            self.assertEqual(rows[identifier]["status"], "interrupted")
            self.assertEqual(rows[identifier]["reasons"], ["restored_inbox_requires_review"])
            self.assertEqual(rows[identifier]["client_order_id"], "opdBackupFixture")
        self.assertIsNone(restored.claim_tradingview_alert("new-worker", 1000))

    def test_recovery_archive_retains_wal_committed_before_crash(self):
        target = self.root / "wal-target.sqlite3"
        StateStore(str(target))
        program = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("PRAGMA wal_autocheckpoint=0")
connection.execute("INSERT INTO control_flags VALUES('wal_survivor', 'true', '', 'fixture')")
connection.commit()
os._exit(0)
"""
        subprocess.run([sys.executable, "-c", program, str(target)], check=True, timeout=15)
        self.assertGreater(Path(str(target) + "-wal").stat().st_size, 0)
        result = maintenance.restore_database(self.source, target, offline=True)
        archive = Path(result["archive"])
        self.assertTrue((archive / "raw" / (target.name + "-wal")).is_file())
        self.assertTrue(StateStore(str(archive / "rollback.sqlite3")).get_control_flag("wal_survivor"))
        manifest = json.loads((archive / "manifest.json").read_text())
        for item in manifest["files"]:
            self.assertEqual(maintenance.file_digest(archive / "raw" / item["name"]), item["sha256"])
        self.assertFalse(Path(str(target) + "-wal").exists())

    def test_invalid_restore_never_touches_current_database(self):
        target = self.root / "target.sqlite3"
        StateStore(str(target))
        before = target.read_bytes()
        invalid = self.root / "bad.sqlite3"
        invalid.write_bytes(b"bad")
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.restore_database(invalid, target, offline=True)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse((self.root / "recovery").exists())

    def test_killed_restore_leaves_durable_startup_barrier(self):
        target = self.root / "target.sqlite3"
        StateStore(str(target))
        program = """
import os, sys
from pathlib import Path
from app import database_maintenance as m
source, target = map(Path, sys.argv[1:])
replace = os.replace
def crash_before_replace(a, b):
    if Path(b) == target:
        os._exit(31)
    return replace(a, b)
m.os.replace = crash_before_replace
m.restore_database(source, target, offline=True)
"""
        process = subprocess.run([sys.executable, "-c", program, str(self.source), str(target)], timeout=15)
        self.assertEqual(process.returncode, 31)
        marker = maintenance.recovery_marker(target)
        self.assertTrue(marker.exists())
        with self.assertRaisesRegex(RuntimeError, "recovery is incomplete"):
            StateStore(str(target))
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.online_snapshot(target, self.root / "unsafe.sqlite3")
        maintenance.restore_database(self.source, target, offline=True)
        self.assertFalse(marker.exists())
        self.assertTrue(SafetyController(StateStore(str(target))).emergency_stopped)

    def test_stdin_transfer_checksum_is_required(self):
        target = self.root / "target.sqlite3"
        environment = {**os.environ, "STATE_DB_PATH": str(target)}
        process = subprocess.run(
            [sys.executable, "-m", "app.database_maintenance", "restore", "--offline", "--sha256", "wrong"],
            input=self.source.read_bytes(), capture_output=True, env=environment, timeout=15,
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b"checksum", process.stderr)
        self.assertFalse(target.exists())

    def test_real_cli_backup_restore_roundtrip_custom_path(self):
        target = self.root / "nested" / "custom.db"
        self.store.set_control_flag("roundtrip", True)
        backup = subprocess.run(
            [sys.executable, "-m", "app.database_maintenance", "backup"],
            capture_output=True, env={**os.environ, "STATE_DB_PATH": str(self.source)}, check=True, timeout=15,
        )
        snapshot = self.root / "snapshot.sqlite3"
        snapshot.write_bytes(backup.stdout)
        result = subprocess.run(
            [sys.executable, "-m", "app.database_maintenance", "restore", "--offline", "--sha256", maintenance.file_digest(snapshot)],
            input=backup.stdout, capture_output=True, env={**os.environ, "STATE_DB_PATH": str(target)}, check=True, timeout=15,
        )
        self.assertTrue(json.loads(result.stdout)["restored"])
        restored = StateStore(str(target))
        self.assertTrue(restored.get_control_flag("roundtrip"))
        self.assertTrue(SafetyController(restored).emergency_stopped)

    def test_runtime_verification_rejects_each_unlocked_condition(self):
        status = {
            "execution_enabled": False,
            "automation_worker": {"enabled": False, "running": False, "dry_run": True},
            "live_safety": {"allowed": False, "configuration_enabled": False},
            "safety_control": {"emergency_stopped": True},
        }
        maintenance.verify_runtime_status(status)
        for group, name in [(None, "execution_enabled"), ("automation_worker", "enabled"), ("automation_worker", "running"),
                            ("automation_worker", "dry_run"), ("live_safety", "allowed"),
                            ("live_safety", "configuration_enabled"), ("safety_control", "emergency_stopped")]:
            altered = json.loads(json.dumps(status))
            container = altered if group is None else altered[group]
            container[name] = not container[name]
            with self.subTest(name=name), self.assertRaises(maintenance.MaintenanceError):
                maintenance.verify_runtime_status(altered)


if __name__ == "__main__":
    unittest.main()
