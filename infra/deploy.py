"""Compose deployment commands; configuration is resolved by Compose itself."""

import argparse
import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from app.database_maintenance import (  # noqa: E402
    MaintenanceError, assert_restore_environment, file_digest,
    fsync_directory, online_snapshot, validate_database, write_json,
)


class DeploymentError(RuntimeError):
    pass


def effective_environment(config: dict) -> dict:
    environment = config.get("services", {}).get("api", {}).get("environment")
    if not isinstance(environment, dict):
        raise DeploymentError("Compose did not resolve an API environment.")
    return {name: str(value) if value is not None else "" for name, value in environment.items()}


def validate_config(config: dict, *, recovery: bool = False) -> dict:
    environment = effective_environment(config)
    failures = []
    booleans = {}
    for name in ("OKX_DEMO", "EXECUTION_ENABLED", "LIVE_TRADING_ENABLED", "AUTO_TRADING_ENABLED", "AUTO_TRADING_DRY_RUN", "TRADINGAGENTS_ENABLED"):
        value = environment.get(name, "").lower()
        if value not in ("true", "false"):
            failures.append(f"{name} must be true or false")
        booleans[name] = value == "true"
    mode = environment.get("TRADING_MODE", "").lower()
    if mode not in ("demo", "live"):
        failures.append("TRADING_MODE must be demo or live")
    elif booleans["OKX_DEMO"] != (mode == "demo"):
        failures.append("OKX_DEMO must match TRADING_MODE")
    if mode == "demo" and booleans["LIVE_TRADING_ENABLED"]:
        failures.append("Demo mode must keep LIVE_TRADING_ENABLED=false")
    if mode == "live" and booleans["EXECUTION_ENABLED"]:
        if not booleans["LIVE_TRADING_ENABLED"] or not environment.get("LIVE_UNLOCK_PHRASE", "").strip():
            failures.append("Live execution requires the separate live gate and unlock phrase")
    if len(environment.get("ADMIN_API_TOKEN", "").strip()) < 16:
        failures.append("ADMIN_API_TOKEN must contain at least 16 characters")
    if booleans["EXECUTION_ENABLED"]:
        if not all(environment.get(name, "").strip() for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")):
            failures.append("Execution requires all three OKX credentials")
    if booleans["AUTO_TRADING_ENABLED"] and not booleans["AUTO_TRADING_DRY_RUN"]:
        if not booleans["EXECUTION_ENABLED"] or mode == "live":
            failures.append("Non-dry-run automation requires enabled Demo execution")
    if booleans["TRADINGAGENTS_ENABLED"] and not environment.get("TRADINGAGENTS_PATH", "").strip():
        failures.append("TradingAgents requires TRADINGAGENTS_PATH")
    for name in ("OKX_PROXY_URL", "PUSHPLUS_PROXY_URL"):
        value = environment.get(name, "")
        try:
            parsed = urllib.parse.urlsplit(value)
            if value and (parsed.scheme not in ("http", "https", "socks5", "socks5h") or not parsed.hostname):
                failures.append(f"{name} must be an HTTP or SOCKS5 proxy URL")
        except ValueError:
            failures.append(f"{name} is not a valid proxy URL")
    path = database_path(environment)
    mounts = config.get("services", {}).get("api", {}).get("volumes", [])
    matching = [
        mount for mount in mounts if isinstance(mount, dict)
        and mount.get("target")
        and path.is_relative_to(PurePosixPath(mount["target"]))
    ]
    mount = max(matching, key=lambda item: len(item["target"])) if matching else {}
    if mount.get("type") not in ("bind", "volume") or mount.get("read_only", False):
        failures.append("State database must be inside a writable persistent API volume")
    if recovery:
        try:
            assert_restore_environment(environment)
        except MaintenanceError as error:
            failures.append(str(error))
    if failures:
        raise DeploymentError("Preflight failed:\n  - " + "\n  - ".join(failures))
    return environment


def database_path(environment: dict) -> PurePosixPath:
    value = environment.get("STATE_DB_PATH") or str(PurePosixPath(environment.get("DATA_DIR") or "/data") / "openperpdesk.sqlite3")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise DeploymentError("State database path must be absolute and normalized inside the container.")
    return path


def binding_origin(binding: str) -> str:
    lines = [line.strip() for line in binding.splitlines() if line.strip()]
    for line in lines:
        try:
            parsed = urllib.parse.urlsplit("http://" + line)
            host, port = parsed.hostname, parsed.port
            if not host or not port or parsed.path or parsed.username or parsed.password:
                continue
            host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
            return f"http://{'[' + host + ']' if ':' in host else host}:{port}"
        except ValueError:
            continue
    raise DeploymentError("No valid web port binding; refusing to guess another local service.")


class Deployment:
    def __init__(self, root: Path = ROOT):
        self.root = root
        self.env_file = Path(os.getenv("OPENPERPDESK_ENV_FILE", root / ".env")).absolute()
        self.backup_dir = Path(os.getenv("OPENPERPDESK_BACKUP_DIR", root / "backups")).absolute()
        self.command = ["docker", "compose", "--env-file", str(self.env_file)]

    def require_environment_file(self) -> None:
        if not self.env_file.is_file():
            raise DeploymentError("Missing environment file. Create .env from .env.example.")
        if stat.S_IMODE(self.env_file.stat().st_mode) != 0o600:
            raise DeploymentError("Environment file permissions must be 600.")

    def compose(self, *args: str, stdin=None, stdout=subprocess.PIPE, timeout=180) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                [*self.command, *args], cwd=self.root, stdin=stdin,
                stdout=stdout, stderr=None if args[0] == "logs" else subprocess.PIPE, timeout=timeout, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            raise DeploymentError("Docker Compose is unavailable or the command timed out.") from error
        if result.returncode:
            # Config interpolation errors may contain secrets, so do not echo raw stderr.
            raise DeploymentError(f"Compose {args[0]} failed (exit {result.returncode}); inspect local Docker configuration or service logs.")
        return result

    def config(self) -> dict:
        self.require_environment_file()
        try:
            return json.loads(self.compose("config", "--format", "json").stdout)
        except (ValueError, TypeError) as error:
            raise DeploymentError("Compose returned invalid configuration JSON.") from error

    def preflight(self, *, recovery: bool = False) -> dict:
        environment = validate_config(self.config(), recovery=recovery)
        print(f"Preflight passed: mode={environment['TRADING_MODE'].lower()}, "
              f"execution={environment['EXECUTION_ENABLED'].lower()}, "
              f"auto_dry_run={environment['AUTO_TRADING_DRY_RUN'].lower()}; secrets redacted.")
        return environment

    @contextmanager
    def lock(self):
        with (self.root / ".openperpdesk-deploy.lock").open("a+b") as stream:
            os.chmod(stream.name, 0o600)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise DeploymentError("Another deployment or recovery command is running.") from error
            yield

    def smoke(self, origin: str | None = None, *, readiness: bool = True) -> None:
        origin = origin or binding_origin(self.compose("port", "web", "80").stdout.decode())
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        paths = ["/", "/assets/icons/chart-candlestick.svg", "/api/v1/health"]
        if readiness:
            paths.append("/api/v1/health/readiness")
        for path in paths:
            with opener.open(origin.rstrip("/") + path, timeout=10) as response:
                data = response.read()
                if path.endswith(".svg") and b"<svg" not in data:
                    raise DeploymentError("Web icon asset is missing from the image.")
                if path == "/api/v1/health" and json.loads(data).get("status") != "ok":
                    raise DeploymentError("API process health check failed.")
                if path.endswith("readiness") and json.loads(data).get("ready") is not True:
                    raise DeploymentError("API readiness check failed.")
        print("Smoke passed: web, local icon asset, API health" + (" and readiness." if readiness else "."))

    def running_maintenance(self, command: str, *, stdout=subprocess.PIPE):
        with (self.root / "services/api/app/database_maintenance.py").open("rb") as program:
            return self.compose("exec", "-T", "api", "python", "-", command, stdin=program, stdout=stdout)

    def api_containers(self) -> list[dict]:
        raw = self.compose("ps", "--all", "--format", "json", "api").stdout
        try:
            text = raw.decode().strip()
            if not text:
                return []
            try:
                rows = json.loads(text)
                rows = rows if isinstance(rows, list) else [rows]
            except json.JSONDecodeError:
                rows = [json.loads(line) for line in text.splitlines() if line.strip()]
            states = {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
            if any(
                not isinstance(row, dict) or not isinstance(row.get("ID"), str) or not row["ID"]
                or row.get("Service") != "api" or row.get("State") not in states
                for row in rows
            ):
                raise ValueError("Invalid API container state")
            if len({row["ID"] for row in rows}) != len(rows):
                raise ValueError("Duplicate API container identity")
            return rows
        except (UnicodeError, ValueError, TypeError) as error:
            raise DeploymentError("Cannot verify API container state; recovery refused.") from error

    def backup(self) -> Path:
        self.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.backup_dir, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.backup_dir / f"openperpdesk-{stamp}-{uuid4().hex[:8]}.sqlite3"
        descriptor, name = tempfile.mkstemp(prefix=".backup-", suffix=".partial", dir=self.backup_dir)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                self.running_maintenance("backup", stdout=output)
                output.flush()
                os.fsync(output.fileno())
            validate_database(temporary)
            digest = file_digest(temporary)
            os.replace(temporary, destination)
            fsync_directory(self.backup_dir)
            write_json(destination.with_suffix(".json"), {
                "format": 1, "created_at": stamp, "sha256": digest,
                "database": destination.name, "bytes": destination.stat().st_size,
            })
        finally:
            temporary.unlink(missing_ok=True)
        print(f"Validated snapshot: {destination}")
        return destination

    def restore(self, source: Path) -> dict:
        environment = self.preflight(recovery=True)
        validate_database(source)
        digest = file_digest(source)
        metadata = source.with_suffix(".json")
        if metadata.exists() and json.loads(metadata.read_text()).get("sha256") != digest:
            raise DeploymentError("Backup manifest checksum does not match the database.")
        with tempfile.TemporaryDirectory(prefix="openperpdesk-recovery-") as directory:
            snapshot = Path(directory) / "snapshot.sqlite3"
            online_snapshot(source, snapshot)
            return self.restore_snapshot(snapshot, environment)

    def restore_snapshot(self, source: Path, environment: dict) -> dict:
        digest = file_digest(source)
        expected = str(database_path(environment))
        helper = ("run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "api", "-m", "app.database_maintenance")
        info = json.loads(self.compose(*helper, "info").stdout)
        if info.get("format") != 1 or info.get("database") != expected:
            raise DeploymentError("Maintenance image does not match the configured state path.")
        containers = self.api_containers()
        if any(row["State"] not in {"running", "exited", "created"} for row in containers):
            raise DeploymentError("API container state is not stable; recovery refused.")
        running = [row for row in containers if row["State"] == "running"]
        if running:
            if len(running) != 1:
                raise DeploymentError("Recovery requires one API instance.")
            current = json.loads(self.running_maintenance("info").stdout)
            if current.get("database") != expected:
                raise DeploymentError("Running API uses a different database path; resolve configuration drift first.")
        self.compose("stop", "api")
        # A stopped container still has an ID. Its lifecycle state proves quiescence.
        if any(row["State"] not in {"exited", "created"} for row in self.api_containers()):
            raise DeploymentError("API is still running; recovery refused.")
        result = None
        try:
            with source.open("rb") as incoming:
                result = json.loads(self.compose(*helper, "restore", "--offline", "--sha256", digest, stdin=incoming).stdout)
            if result.get("restored") is not True:
                raise DeploymentError("Maintenance did not confirm recovery.")
            print(f"Prior database retained in container volume: {result['archive']}")
            self.compose("up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "90", "api")
            self.compose("exec", "-T", "api", "python", "-m", "app.database_maintenance", "verify-runtime")
            self.compose("up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "90", "web")
            self.smoke(readiness=False)
        except BaseException:
            try:
                self.compose("stop", "api")
            except DeploymentError:
                print("Could not confirm API stop; inspect Docker immediately.", file=sys.stderr)
            if result:
                print(f"Recovery not accepted; rollback archive: {result['archive']}", file=sys.stderr)
            raise
        print("Recovery verified: execution disabled, worker disabled, dry run enabled, emergency stop active.")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("up", "down", "restart", "status", "preflight", "backup"):
        commands.add_parser(name)
    commands.add_parser("smoke").add_argument("origin", nargs="?")
    commands.add_parser("restore").add_argument("source", type=Path)
    commands.add_parser("logs").add_argument("service", nargs="?")
    args = parser.parse_args()
    deployment = Deployment()
    deployment.require_environment_file()
    if args.command == "preflight":
        deployment.preflight()
    elif args.command == "smoke":
        deployment.smoke(args.origin)
    elif args.command in ("status", "logs"):
        arguments = ["ps"] if args.command == "status" else ["logs", "-f", "--tail=200", *([args.service] if args.service else [])]
        deployment.compose(*arguments, stdout=None, timeout=None)
    else:
        with deployment.lock():
            if args.command == "backup":
                deployment.backup()
            elif args.command == "restore":
                deployment.restore(args.source.absolute())
            elif args.command == "down":
                deployment.compose("down", stdout=None)
            else:
                deployment.preflight()
                deployment.compose("up", "-d", "--build", "--wait", "--wait-timeout", "90", stdout=None, timeout=None)
                deployment.compose("exec", "-T", "web", "nginx", "-s", "reload")


if __name__ == "__main__":
    try:
        main()
    except (DeploymentError, MaintenanceError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
