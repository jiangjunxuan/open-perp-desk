import importlib.util
import hashlib
import io
import json
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.database_maintenance import MaintenanceError, file_digest
from app.state_store import StateStore

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("openperpdesk_deploy", ROOT / "infra/deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def configuration():
    return {"name": "openperpdesk", "services": {"api": {
        "environment": {
            "TRADING_MODE": "demo", "OKX_DEMO": "true",
            "EXECUTION_ENABLED": "false", "LIVE_TRADING_ENABLED": "false",
            "AUTO_TRADING_ENABLED": "false", "AUTO_TRADING_DRY_RUN": "true",
            "TRADINGAGENTS_ENABLED": "false", "ADMIN_API_TOKEN": "fixture-secret-never-log",
            "DATA_DIR": "/data", "STATE_DB_PATH": "/data/nested/custom.db",
        },
        "volumes": [{"type": "volume", "source": "state", "target": "/data"}],
    }}}


class FakeDeployment(deploy.Deployment):
    """Command contract fixture, not evidence of a running Docker engine."""

    def __init__(self, root):
        super().__init__(root)
        self.env_file = root / ".env"
        self.env_file.write_text("EXECUTION_ENABLED=false\n")
        self.env_file.chmod(0o600)
        self.backup_dir = root / "backups"
        self.model = configuration()
        self.calls = []
        self.running = True
        self.fail_runtime = False
        self.stays_running = False
        self.snapshot = root / "source.sqlite3"
        StateStore(str(self.snapshot))
        helper = root / "services/api/app/database_maintenance.py"
        helper.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "services/api/app/database_maintenance.py", helper)

    def compose(self, *args, **kwargs):
        self.calls.append(args)
        output = b""
        if args[0] == "config":
            output = json.dumps(self.model).encode()
        elif args[0] == "ps":
            output = b"fixture-api\n" if self.running else b""
        elif args[0] == "stop":
            if not self.stays_running:
                self.running = False
        elif args[-1] == "info":
            output = json.dumps({"format": 1, "database": self.model["services"]["api"]["environment"]["STATE_DB_PATH"]}).encode()
        elif "restore" in args:
            self.asserted_transfer = kwargs["stdin"].read()
            self.asserted_digest = args[-1]
            output = json.dumps({"restored": True, "archive": "/data/recovery/fixture", "emergency_stopped": True}).encode()
        elif args[-1] == "backup":
            kwargs["stdout"].write(self.snapshot.read_bytes())
        elif args[-1] == "verify-runtime":
            if self.fail_runtime:
                raise deploy.DeploymentError("runtime fixture failure")
            output = b'{"execution_locked":true}'
        elif args[0] == "up" and args[-1] == "api":
            self.running = True
        return subprocess.CompletedProcess(args, 0, stdout=output)

    def smoke(self, origin=None, *, readiness=True):
        self.calls.append(("smoke", readiness))


class DeploymentConfigTests(unittest.TestCase):
    def test_config_accepts_locked_demo_and_locked_live(self):
        model = configuration()
        deploy.validate_config(model, recovery=True)
        model["services"]["api"]["environment"].update(TRADING_MODE="live", OKX_DEMO="false")
        deploy.validate_config(model, recovery=True)

    def test_unsafe_mode_combinations_rejected(self):
        for changes in (
            {"OKX_DEMO": "false"}, {"LIVE_TRADING_ENABLED": "true"},
            {"TRADING_MODE": "invalid"}, {"EXECUTION_ENABLED": "yes"},
            {"EXECUTION_ENABLED": "true"}, {"ADMIN_API_TOKEN": "short"},
            {"AUTO_TRADING_ENABLED": "true", "AUTO_TRADING_DRY_RUN": "false"},
            {"TRADINGAGENTS_ENABLED": "true"}, {"OKX_PROXY_URL": "file:///private"},
        ):
            model = configuration()
            model["services"]["api"]["environment"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(deploy.DeploymentError):
                deploy.validate_config(model)

    def test_restore_rejects_worker_even_when_it_is_dry_run(self):
        model = configuration()
        model["services"]["api"]["environment"]["AUTO_TRADING_ENABLED"] = "true"
        deploy.validate_config(model)
        with self.assertRaises(deploy.DeploymentError):
            deploy.validate_config(model, recovery=True)

    def test_state_path_requires_effective_writable_persistence(self):
        for path, extra in (
            ("/tmp/state.sqlite3", []),
            ("relative.sqlite3", []),
            ("/data/../tmp/state.sqlite3", []),
            ("/data/nested/state.sqlite3", [{"type": "tmpfs", "target": "/data/nested"}]),
            ("/data/nested/state.sqlite3", [{"type": "bind", "target": "/data/nested", "read_only": True}]),
        ):
            model = configuration()
            model["services"]["api"]["environment"]["STATE_DB_PATH"] = path
            model["services"]["api"]["volumes"].extend(extra)
            with self.subTest(path=path, extra=extra), self.assertRaises(deploy.DeploymentError):
                deploy.validate_config(model)

    def test_binding_formats_and_no_guessed_service(self):
        for binding, expected in {
            "127.0.0.1:8080": "http://127.0.0.1:8080",
            "0.0.0.0:8080": "http://127.0.0.1:8080",
            "[::]:8080": "http://[::1]:8080",
            "[::1]:8090": "http://[::1]:8090",
            "0.0.0.0:8080\n[::]:8080\n": "http://127.0.0.1:8080",
        }.items():
            with self.subTest(binding=binding):
                self.assertEqual(deploy.binding_origin(binding), expected)
        for binding in ("", "8080", "127.0.0.1:99999", "127.0.0.1:8080/not-a-port"):
            with self.subTest(binding=binding), self.assertRaises(deploy.DeploymentError):
                deploy.binding_origin(binding)


class DeploymentCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.deployment = FakeDeployment(self.root)
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_preflight_uses_resolved_compose_not_literal_dotenv(self):
        self.deployment.model["services"]["api"]["environment"]["EXECUTION_ENABLED"] = "true"
        with self.assertRaises(deploy.DeploymentError):
            self.deployment.preflight()
        self.assertEqual(self.deployment.calls, [("config", "--format", "json")])
        self.assertNotIn("fixture-secret", self.output.getvalue())

    def test_preflight_enforces_environment_file_permissions(self):
        self.deployment.env_file.chmod(0o644)
        with self.assertRaisesRegex(deploy.DeploymentError, "600"):
            self.deployment.preflight()
        self.assertFalse(self.deployment.calls)

    def test_backup_is_validated_private_and_has_checksum_manifest(self):
        result = self.deployment.backup()
        self.assertEqual(result.read_bytes(), self.deployment.snapshot.read_bytes())
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(result.parent.stat().st_mode), 0o700)
        self.assertEqual(json.loads(result.with_suffix(".json").read_text())["sha256"], file_digest(result))
        self.assertFalse(list(result.parent.glob("*.partial")))
        self.assertEqual(self.deployment.calls[0], ("exec", "-T", "api", "python", "-", "backup"))

    def test_failed_transfer_does_not_publish_backup(self):
        self.deployment.snapshot.write_bytes(b"partial transfer")
        with self.assertRaises(MaintenanceError):
            self.deployment.backup()
        self.assertEqual(list(self.deployment.backup_dir.iterdir()), [])

    def test_restore_recreates_both_services_and_verifies_locks(self):
        result = self.deployment.restore(self.deployment.snapshot)
        self.assertTrue(result["restored"])
        commands = self.deployment.calls
        stop = commands.index(("stop", "api"))
        restore = next(index for index, args in enumerate(commands) if "restore" in args)
        recreate = next(index for index, args in enumerate(commands) if args[0] == "up" and args[-1] == "api")
        verify = next(index for index, args in enumerate(commands) if args[-1] == "verify-runtime")
        web = next(index for index, args in enumerate(commands) if args[0] == "up" and args[-1] == "web")
        self.assertLess(stop, restore)
        self.assertLess(restore, recreate)
        self.assertLess(recreate, verify)
        self.assertLess(verify, web)
        self.assertIn("--force-recreate", commands[recreate])
        self.assertNotIn(("start", "api"), commands)
        self.assertEqual(commands[-1], ("smoke", False))
        self.assertEqual(self.deployment.asserted_digest, hashlib.sha256(self.deployment.asserted_transfer).hexdigest())

    def test_restore_stages_wal_source_before_binary_transfer(self):
        with closing(sqlite3.connect(self.deployment.snapshot)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("UPDATE strategies SET enabled=1")
            connection.commit()
            self.deployment.restore(self.deployment.snapshot)
        staged = self.root / "transferred.sqlite3"
        staged.write_bytes(self.deployment.asserted_transfer)
        with closing(sqlite3.connect(staged)) as connection:
            self.assertEqual(connection.execute("SELECT enabled FROM strategies").fetchone(), (1,))

    def test_restore_validation_or_unsafe_config_never_stops_api(self):
        original = self.deployment.snapshot.read_bytes()
        self.deployment.snapshot.write_bytes(b"invalid")
        with self.assertRaises(MaintenanceError):
            self.deployment.restore(self.deployment.snapshot)
        self.assertNotIn(("stop", "api"), self.deployment.calls)
        self.deployment.snapshot.write_bytes(original)
        self.deployment.model["services"]["api"]["environment"]["AUTO_TRADING_ENABLED"] = "true"
        with self.assertRaises(deploy.DeploymentError):
            self.deployment.restore(self.deployment.snapshot)
        self.assertNotIn(("stop", "api"), self.deployment.calls)

    def test_restore_rejects_corrupt_manifest_before_stop(self):
        self.deployment.snapshot.with_suffix(".json").write_text('{"sha256":"incorrect"}')
        with self.assertRaisesRegex(deploy.DeploymentError, "checksum"):
            self.deployment.restore(self.deployment.snapshot)
        self.assertNotIn(("stop", "api"), self.deployment.calls)

    def test_restore_refuses_if_api_did_not_stop(self):
        self.deployment.stays_running = True
        with self.assertRaisesRegex(deploy.DeploymentError, "still running"):
            self.deployment.restore(self.deployment.snapshot)
        self.assertFalse(any("restore" in args for args in self.deployment.calls))

    def test_runtime_gate_failure_stops_api_and_preserves_archive(self):
        self.deployment.fail_runtime = True
        with self.assertRaises(deploy.DeploymentError):
            self.deployment.restore(self.deployment.snapshot)
        self.assertEqual(self.deployment.calls[-1], ("stop", "api"))
        self.assertFalse(self.deployment.running)
        self.assertFalse(any(args[-1] == "web" for args in self.deployment.calls))

    def test_compose_failure_does_not_echo_config_secrets(self):
        instance = deploy.Deployment(self.root)
        with patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 1, stdout=b"", stderr=b"private-key-in-interpolation-error",
        )):
            with self.assertRaises(deploy.DeploymentError) as captured:
                instance.compose("config", "--format", "json")
        self.assertNotIn("private-key", str(captured.exception))

    def test_restore_maintenance_runtime_path_drift_is_rejected(self):
        original = self.deployment.running_maintenance
        self.deployment.running_maintenance = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout=b'{"format":1,"database":"/different/old.sqlite3"}',
        )
        with self.assertRaisesRegex(deploy.DeploymentError, "configuration drift"):
            self.deployment.restore(self.deployment.snapshot)
        self.assertNotIn(("stop", "api"), self.deployment.calls)
        self.deployment.running_maintenance = original


if __name__ == "__main__":
    unittest.main()
