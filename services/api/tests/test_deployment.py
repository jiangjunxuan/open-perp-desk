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
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

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


def private_report():
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "private_rest_and_websocket_read_only",
        "demo": True, "proxy_configured": False,
        "private_ws_connected": True, "private_ws_authenticated": True,
        "algo_ws_connected": True, "algo_ws_authenticated": True,
        "trading_performed": False, "order_lifecycle_verified": False,
        "rows": {name: 0 for name in (
            "balance", "positions", "config", "pending_orders",
            "orders_history", "fills_history", "pending_algo_orders",
        )},
    }


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
        self.probe_changes = {}
        self.snapshot = root / "source.sqlite3"
        StateStore(str(self.snapshot))
        helper = root / "services/api/app/database_maintenance.py"
        helper.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "services/api/app/database_maintenance.py", helper)
        (root / "infra").mkdir()
        shutil.copyfile(ROOT / "infra/okx-private-smoke.py", root / "infra/okx-private-smoke.py")

    def compose(self, *args, **kwargs):
        self.calls.append(args)
        output = b""
        if args[0] == "config":
            output = json.dumps(self.model).encode()
        elif args[0] == "ps":
            output = json.dumps([{
                "ID": "fixture-api", "Service": "api",
                "State": "running" if self.running else "exited",
            }]).encode()
        elif args[0] == "stop":
            if not self.stays_running:
                self.running = False
        elif "--output" in args:
            self.probe_program = kwargs["stdin"].read()
            self.probe_timeout = kwargs["timeout"]
            output = json.dumps({**private_report(), **self.probe_changes}).encode()
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
            {"EXECUTION_ENABLED": "true"},
            {"AUTO_TRADING_ENABLED": "true", "AUTO_TRADING_DRY_RUN": "false"},
            {"TRADINGAGENTS_ENABLED": "true"}, {"OKX_PROXY_URL": "file:///private"},
        ):
            model = configuration()
            model["services"]["api"]["environment"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(deploy.DeploymentError):
                deploy.validate_config(model)

    def test_short_local_admin_token_is_development_only(self):
        model = configuration()
        model["services"]["api"]["environment"]["ADMIN_API_TOKEN"] = "admin"
        deploy.validate_config(model)
        model["services"]["api"]["environment"]["APP_ENV"] = "production"
        with self.assertRaisesRegex(deploy.DeploymentError, "outside development/test"):
            deploy.validate_config(model)

    def test_empty_admin_token_is_rejected_in_every_environment(self):
        model = configuration()
        model["services"]["api"]["environment"]["ADMIN_API_TOKEN"] = ""
        with self.assertRaisesRegex(deploy.DeploymentError, "must be configured"):
            deploy.validate_config(model)

    def test_tradingview_preview_and_enabled_demo_execution_configuration(self):
        model = configuration()
        environment = model["services"]["api"]["environment"]
        environment.update(
            APP_ENV="production", TRADINGVIEW_ENABLED="true",
            TRADINGVIEW_WEBHOOK_SECRET="fixture-webhook-secret-never-log-32",
            TRADINGVIEW_SYMBOLS="BTC-USDT-SWAP,ETH-USDT-SWAP",
        )
        deploy.validate_config(model)
        environment.update(
            TRADINGVIEW_EXECUTION_ENABLED="true", TRADINGVIEW_DRY_RUN="false",
            EXECUTION_ENABLED="true", OKX_API_KEY="fixture", OKX_SECRET_KEY="fixture",
            OKX_PASSPHRASE="fixture",
        )
        deploy.validate_config(model)

    def test_tradingview_invalid_flags_and_missing_global_gate_rejected(self):
        for changes in (
            {"TRADINGVIEW_ENABLED": "yes"}, {"TRADINGVIEW_EXECUTION_ENABLED": "1"},
            {"TRADINGVIEW_DRY_RUN": "tru"}, {"TRADINGVIEW_DRY_RUN": ""},
            {"TRADINGVIEW_ENABLED": "true"},
            {"TRADINGVIEW_ENABLED": "true", "TRADINGVIEW_WEBHOOK_SECRET": "fixture",
             "TRADINGVIEW_EXECUTION_ENABLED": "true", "TRADINGVIEW_DRY_RUN": "false"},
        ):
            model = configuration()
            model["services"]["api"]["environment"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(deploy.DeploymentError):
                deploy.validate_config(model)

    def test_tradingview_production_secret_and_allowlist_validation_redacts_values(self):
        for changes in (
            {"TRADINGVIEW_WEBHOOK_SECRET": "fixture-secret"},
            {"TRADINGVIEW_WEBHOOK_SECRET": "x" * 257},
            {"TRADINGVIEW_SYMBOLS": ""}, {"TRADINGVIEW_SYMBOLS": " , "},
            {"TRADINGVIEW_SYMBOLS": "BTCUSDT"}, {"TRADINGVIEW_SYMBOLS": "BTC-USDT-SWAP,ETHUSDT"},
        ):
            model = configuration()
            environment = model["services"]["api"]["environment"]
            environment.update(
                APP_ENV="production", TRADINGVIEW_ENABLED="true",
                TRADINGVIEW_WEBHOOK_SECRET="fixture-webhook-secret-never-log-32",
            )
            environment.update(changes)
            with self.subTest(changes=changes), self.assertRaises(deploy.DeploymentError) as raised:
                deploy.validate_config(model)
            self.assertNotIn(environment["TRADINGVIEW_WEBHOOK_SECRET"], str(raised.exception))

    def test_tradingview_numeric_parameters_must_be_finite_and_usable(self):
        for name, value in (
            ("TRADINGVIEW_DEFAULT_SIZE", "0"), ("TRADINGVIEW_DEFAULT_SIZE", "nan"),
            ("TRADINGVIEW_ACCOUNT_EQUITY", "-1"), ("TRADINGVIEW_DAILY_PNL_PCT", "inf"),
            ("TRADINGVIEW_CURRENT_NOTIONAL", "-1"), ("TRADINGVIEW_MAX_AGE_SECONDS", "14"),
            ("TRADINGVIEW_SIGNAL_TTL_SECONDS", "3601"), ("TRADINGVIEW_MAX_AGE_SECONDS", "30.5"),
        ):
            model = configuration()
            model["services"]["api"]["environment"].update(
                TRADINGVIEW_ENABLED="true", TRADINGVIEW_WEBHOOK_SECRET="fixture",
            )
            model["services"]["api"]["environment"][name] = value
            with self.subTest(name=name, value=value), self.assertRaisesRegex(deploy.DeploymentError, name):
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

    def test_prebuilt_mode_requires_a_compose_overlay(self):
        model = configuration()
        environment = model["services"]["api"]["environment"]
        environment["OPENPERPDESK_USE_PREBUILT_IMAGES"] = "true"
        deploy.validate_config(model)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "OPENPERPDESK_COMPOSE_OVERLAY=missing.yml\n",
                encoding="utf-8",
            )
            (root / ".env").chmod(0o600)
            deployment = deploy.Deployment(root)
            deployment.model = model
            deployment.config = lambda: model
            with self.assertRaisesRegex(deploy.DeploymentError, "does not exist"):
                deployment.preflight()

    def test_env_file_selector_is_not_shell_evaluated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "OPENPERPDESK_COMPOSE_OVERLAY='docker-compose.release.yml'\n"
                "RUN_THIS=$(touch /tmp/openperpdesk-should-not-exist)\n",
                encoding="utf-8",
            )
            self.assertEqual(
                deploy.env_file_value(path, "OPENPERPDESK_COMPOSE_OVERLAY"),
                "docker-compose.release.yml",
            )


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

    def test_prebuilt_preflight_reports_mode_without_building(self):
        environment = self.deployment.model["services"]["api"]["environment"]
        environment["OPENPERPDESK_USE_PREBUILT_IMAGES"] = "true"
        overlay = self.root / "release.yml"
        overlay.write_text("services: {}\n", encoding="utf-8")
        self.deployment.overlay_file = overlay
        result = self.deployment.preflight()
        self.assertEqual(result["OPENPERPDESK_USE_PREBUILT_IMAGES"], "true")
        self.assertIn("prebuilt_images=true", self.output.getvalue())

    def test_prebuilt_restart_does_not_request_a_build(self):
        environment = self.deployment.model["services"]["api"]["environment"]
        environment["OPENPERPDESK_USE_PREBUILT_IMAGES"] = "true"
        overlay = self.root / "release.yml"
        overlay.write_text("services: {}\n", encoding="utf-8")
        self.deployment.overlay_file = overlay
        with patch.object(deploy.sys, "argv", ["deploy", "restart"]), \
             patch.object(deploy, "Deployment", return_value=self.deployment):
            deploy.main()
        up_calls = [call for call in self.deployment.calls if call and call[0] == "up"]
        self.assertEqual(len(up_calls), 1)
        self.assertNotIn("--build", up_calls[0])

    def test_preflight_enforces_environment_file_permissions(self):
        self.deployment.env_file.chmod(0o644)
        with self.assertRaisesRegex(deploy.DeploymentError, "600"):
            self.deployment.preflight()
        self.assertFalse(self.deployment.calls)

    def test_smoke_verifies_private_api_authentication_without_printing_token(self):
        requests = []

        class Response:
            def __init__(self, status, data):
                self.status = status
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return self.data

        class Opener:
            def open(self, request, timeout):
                del timeout
                path = request.full_url.removeprefix("http://fixture")
                requests.append((path, request.headers.get("X-admin-token")))
                if path == "/api/v1/account/overview":
                    if request.headers.get("X-admin-token") != "fixture-secret-never-log":
                        raise HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO(b"{}"))
                    return Response(200, b"{}")
                if path.endswith(".svg"):
                    return Response(200, b"<svg></svg>")
                if path.endswith("/health"):
                    return Response(200, b'{"status":"ok"}')
                if path.endswith("/readiness"):
                    return Response(200, b'{"ready":true}')
                return Response(200, b"{}")

        with patch.object(self.deployment, "config", return_value=self.deployment.model), \
             patch.object(deploy.urllib.request, "build_opener", return_value=Opener()):
            deploy.Deployment.smoke(self.deployment, "http://fixture", readiness=True)

        self.assertEqual(
            [token for path, token in requests if path == "/api/v1/account/overview"],
            [None, "fixture-secret-never-log"],
        )
        self.assertNotIn("fixture-secret-never-log", self.output.getvalue())

    def test_private_smoke_uses_running_container_environment_and_private_report(self):
        result = self.deployment.private_smoke(timeout=20)
        self.assertEqual(self.deployment.calls, [
            ("exec", "-T", "api", "python", "-", "--timeout", "20", "--output", "-"),
        ])
        self.assertEqual(self.deployment.probe_timeout, 35)
        self.assertIn(b"run_probe", self.deployment.probe_program)
        self.assertNotIn(b"fixture-secret", self.deployment.probe_program)
        destination = self.root / "outputs/okx-private-verification.json"
        self.assertEqual(json.loads(destination.read_text()), result)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_private_smoke_rejects_stale_unsafe_and_secret_bearing_reports(self):
        destination = self.root / "outputs/okx-private-verification.json"
        destination.parent.mkdir()
        for changes in (
            {"demo": False}, {"trading_performed": True}, {"order_lifecycle_verified": True},
            {"private_ws_authenticated": False}, {"algo_ws_connected": False},
            {"demo": "true"}, {"checked_at": "2000-01-01T00:00:00+00:00"},
            {"checked_at": "2000-01-01T00:00:00"}, {"rows": {"balance": -1}},
            {"rows": {**private_report()["rows"], "balance": True}},
            {"api_key": "fixture-secret-never-print"},
        ):
            with self.subTest(changes=changes):
                self.deployment.probe_changes = changes
                destination.write_text('{"old":true}')
                with self.assertRaises(deploy.DeploymentError):
                    self.deployment.private_smoke()
                self.assertFalse(destination.exists())
        self.assertNotIn("fixture-secret-never-print", self.output.getvalue())

    def test_private_smoke_live_requires_explicit_readonly_acknowledgement(self):
        self.deployment.probe_changes = {"demo": False}
        report = self.deployment.private_smoke(allow_live=True)
        self.assertFalse(report["demo"] or report["trading_performed"])
        self.assertEqual(self.deployment.calls[-1][-1], "--allow-live")

    def test_private_smoke_cli_and_invalid_timeout(self):
        for timeout in (0, 301, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(deploy.DeploymentError):
                self.deployment.private_smoke(timeout=timeout)
        self.assertEqual(self.deployment.calls, [])
        with patch.object(deploy.sys, "argv", ["deploy", "private-smoke", "--timeout", "12"]), \
             patch.object(deploy, "Deployment", return_value=self.deployment):
            deploy.main()
        self.assertEqual(self.deployment.probe_timeout, 27)
        self.assertNotIn("--allow-live", self.deployment.calls[-1])

    def test_failed_container_private_probe_removes_previous_report(self):
        destination = self.root / "outputs/okx-private-verification.json"
        destination.parent.mkdir()
        destination.write_text('{"old":true}')
        with patch.object(self.deployment, "compose", side_effect=deploy.DeploymentError("fixture failed")), \
             self.assertRaises(deploy.DeploymentError):
            self.deployment.private_smoke()
        self.assertFalse(destination.exists())

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

    def test_stopped_container_identity_does_not_block_restore(self):
        self.deployment.running = False
        result = self.deployment.restore(self.deployment.snapshot)
        self.assertTrue(result["restored"])
        self.assertNotIn(("exec", "-T", "api", "python", "-", "info"), self.deployment.calls)
        self.assertEqual(
            [args for args in self.deployment.calls if args[0] == "ps"],
            [("ps", "--all", "--format", "json", "api")] * 2,
        )

    def test_container_state_supports_compose_json_array_and_json_lines(self):
        rows = [
            {"ID": "first", "Service": "api", "State": "running"},
            {"ID": "old", "Service": "api", "State": "exited"},
        ]
        for payload in (json.dumps(rows), "\n".join(json.dumps(row) for row in rows)):
            with self.subTest(payload=payload), patch.object(self.deployment, "compose", return_value=subprocess.CompletedProcess(
                [], 0, stdout=payload.encode(),
            )):
                self.assertEqual(self.deployment.api_containers(), rows)

    def test_unverifiable_container_states_never_stop_or_restore(self):
        for payload in (
            b"not-json", b'{"ID":"api"}', b"null",
            b'[{"ID":"api","Service":"web","State":"running"}]',
            b'[{"ID":"api","Service":"api","State":"unknown"}]',
        ):
            original = self.deployment.compose
            def reply(*args, **kwargs):
                if args[0] == "ps":
                    return subprocess.CompletedProcess(args, 0, stdout=payload)
                return original(*args, **kwargs)
            with self.subTest(payload=payload), patch.object(self.deployment, "compose", side_effect=reply):
                with self.assertRaisesRegex(deploy.DeploymentError, "Cannot verify"):
                    self.deployment.restore(self.deployment.snapshot)
            self.assertNotIn(("stop", "api"), self.deployment.calls)
            self.assertFalse(any("restore" in args for args in self.deployment.calls))

    def test_paused_restarting_and_removing_containers_are_not_quiescent(self):
        for state in ("paused", "restarting", "removing", "dead"):
            with self.subTest(state=state), patch.object(self.deployment, "api_containers", return_value=[
                {"ID": "api", "Service": "api", "State": state},
            ]):
                with self.assertRaisesRegex(deploy.DeploymentError, "not stable"):
                    self.deployment.restore(self.deployment.snapshot)
            self.assertNotIn(("stop", "api"), self.deployment.calls)
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
