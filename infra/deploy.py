"""Compose deployment commands; configuration is resolved by Compose itself."""

import argparse
import fcntl
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.error
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


def env_file_value(path: Path, name: str) -> str:
    """Read one non-secret selector without evaluating the environment file."""
    if not path.is_file():
        return ""
    prefix = f"{name}="
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or not line.startswith(prefix):
                continue
            value = line[len(prefix):].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    except (OSError, UnicodeError):
        return ""
    return ""


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
    admin_token = environment.get("ADMIN_API_TOKEN", "").strip()
    app_env = environment.get("APP_ENV", "development").strip().lower()
    if not admin_token:
        failures.append("ADMIN_API_TOKEN must be configured")
    elif len(admin_token) < 16 and app_env not in {"development", "dev", "test"}:
        failures.append("ADMIN_API_TOKEN must contain at least 16 characters outside development/test")
    if booleans["EXECUTION_ENABLED"]:
        if not all(environment.get(name, "").strip() for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")):
            failures.append("Execution requires all three OKX credentials")
    if booleans["AUTO_TRADING_ENABLED"] and not booleans["AUTO_TRADING_DRY_RUN"]:
        if not booleans["EXECUTION_ENABLED"] or mode == "live":
            failures.append("Non-dry-run automation requires enabled Demo execution")
    if booleans["TRADINGAGENTS_ENABLED"] and not environment.get("TRADINGAGENTS_PATH", "").strip():
        failures.append("TradingAgents requires TRADINGAGENTS_PATH")
    prebuilt = environment.get("OPENPERPDESK_USE_PREBUILT_IMAGES", "false").strip().lower()
    if prebuilt not in {"true", "false"}:
        failures.append("OPENPERPDESK_USE_PREBUILT_IMAGES must be true or false")
    for name, default in {
        "TRADINGVIEW_ENABLED": "false",
        "TRADINGVIEW_EXECUTION_ENABLED": "false",
        "TRADINGVIEW_DRY_RUN": "true",
    }.items():
        value = environment.get(name, default).strip().lower()
        if value not in {"true", "false"}:
            failures.append(f"{name} must be true or false")
        booleans[name] = value == "true"
    if booleans["TRADINGVIEW_ENABLED"]:
        secret = environment.get("TRADINGVIEW_WEBHOOK_SECRET", "").strip()
        if not secret or len(secret) > 256:
            failures.append("TradingView requires a webhook secret of 1 to 256 characters")
        elif len(secret) < 32 and app_env not in {"development", "dev", "test"}:
            failures.append("TRADINGVIEW_WEBHOOK_SECRET must contain at least 32 characters outside development/test")
        symbols = environment.get(
            "TRADINGVIEW_SYMBOLS", environment.get("MARKET_SYMBOLS", "BTC-USDT-SWAP,ETH-USDT-SWAP"),
        ).split(",")
        if not any(symbol.strip() for symbol in symbols) or any(
            symbol.strip() and not re.fullmatch(r"[A-Z0-9]+-[A-Z0-9]+-SWAP", symbol.strip().upper())
            for symbol in symbols
        ):
            failures.append("TRADINGVIEW_SYMBOLS must contain a nonempty OKX perpetual instrument allowlist")
        if booleans["TRADINGVIEW_EXECUTION_ENABLED"] and not booleans["TRADINGVIEW_DRY_RUN"] and not booleans["EXECUTION_ENABLED"]:
            failures.append("Non-dry-run TradingView execution requires EXECUTION_ENABLED=true")
        for name in ("TRADINGVIEW_SIGNAL_TTL_SECONDS", "TRADINGVIEW_MAX_AGE_SECONDS"):
            try:
                if not 15 <= int(environment.get(name, "300")) <= 3600:
                    raise ValueError
            except ValueError:
                failures.append(f"{name} must be an integer between 15 and 3600")
        for name, default, lower, inclusive in (
            ("TRADINGVIEW_DEFAULT_SIZE", "1", 0, False),
            ("TRADINGVIEW_ACCOUNT_EQUITY", environment.get("AUTO_TRADING_ACCOUNT_EQUITY", "1000"), 0, False),
            ("TRADINGVIEW_DAILY_PNL_PCT", "0", None, True),
            ("TRADINGVIEW_CURRENT_NOTIONAL", "0", 0, True),
        ):
            try:
                number = float(environment.get(name, default))
                if not math.isfinite(number) or (
                    lower is not None and (number < lower if inclusive else number <= lower)
                ):
                    raise ValueError
            except ValueError:
                failures.append(f"{name} must be finite and within its permitted range")
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
        overlay = os.getenv("OPENPERPDESK_COMPOSE_OVERLAY", "").strip()
        if not overlay:
            overlay = env_file_value(self.env_file, "OPENPERPDESK_COMPOSE_OVERLAY").strip()
        self.overlay_file = None
        if overlay:
            candidate = Path(overlay)
            self.overlay_file = candidate if candidate.is_absolute() else self.root / candidate
            self.overlay_file = self.overlay_file.absolute()
            if not self.overlay_file.is_relative_to(self.root):
                raise DeploymentError("Compose overlay must be inside the project directory.")
        self.command = [
            "docker", "compose", "--env-file", str(self.env_file),
            "-f", str(self.root / "docker-compose.yml"),
        ]
        if self.overlay_file:
            self.command.extend(["-f", str(self.overlay_file)])

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
        if environment.get("OPENPERPDESK_USE_PREBUILT_IMAGES", "false").lower() == "true":
            if self.overlay_file is None:
                raise DeploymentError("Prebuilt image mode requires OPENPERPDESK_COMPOSE_OVERLAY.")
            if not self.overlay_file.is_file():
                raise DeploymentError("Configured Compose overlay does not exist.")
        print(f"Preflight passed: mode={environment['TRADING_MODE'].lower()}, "
              f"execution={environment['EXECUTION_ENABLED'].lower()}, "
              f"auto_dry_run={environment['AUTO_TRADING_DRY_RUN'].lower()}, "
              f"prebuilt_images={environment.get('OPENPERPDESK_USE_PREBUILT_IMAGES', 'false').lower()}; "
              "secrets redacted.")
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
        environment = effective_environment(self.config())
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def fetch(path: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
            request = urllib.request.Request(origin.rstrip("/") + path, headers=headers or {})
            try:
                with opener.open(request, timeout=10) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                try:
                    return error.code, error.read()
                finally:
                    error.close()

        paths = ["/", "/assets/icons/chart-candlestick.svg", "/api/v1/health"]
        if readiness:
            paths.append("/api/v1/health/readiness")
        for path in paths:
            status, data = fetch(path)
            if status != 200:
                raise DeploymentError(f"Smoke request failed: {path} returned HTTP {status}.")
            if path.endswith(".svg") and b"<svg" not in data:
                raise DeploymentError("Web icon asset is missing from the image.")
            if path == "/api/v1/health" and json.loads(data).get("status") != "ok":
                raise DeploymentError("API process health check failed.")
            if path.endswith("readiness") and json.loads(data).get("ready") is not True:
                raise DeploymentError("API readiness check failed.")

        admin_token = environment.get("ADMIN_API_TOKEN", "").strip()
        if not admin_token:
            raise DeploymentError("Smoke cannot verify private API access without an admin token.")
        unauthenticated, _ = fetch("/api/v1/account/overview")
        if unauthenticated != 401:
            raise DeploymentError("Private API did not reject an unauthenticated request.")
        authenticated, _ = fetch(
            "/api/v1/account/overview",
            {"X-Admin-Token": admin_token},
        )
        if authenticated not in {200, 502}:
            raise DeploymentError("Configured admin token could not pass private API authentication.")
        print(
            "Smoke passed: web, local icon asset, API health, private API authentication"
            + (" and readiness." if readiness else "."),
        )

    def running_maintenance(self, command: str, *, stdout=subprocess.PIPE):
        with (self.root / "services/api/app/database_maintenance.py").open("rb") as program:
            return self.compose("exec", "-T", "api", "python", "-", command, stdin=program, stdout=stdout)

    def private_smoke(self, *, timeout: float = 45, allow_live: bool = False) -> dict:
        if not 5 <= timeout <= 300:
            raise DeploymentError("Private smoke timeout must be between 5 and 300 seconds.")
        output = self.root / "outputs/okx-private-verification.json"
        output.unlink(missing_ok=True)
        self.require_environment_file()
        started = datetime.now(timezone.utc)
        with (self.root / "infra/okx-private-smoke.py").open("rb") as program:
            result = self.compose(
                "exec", "-T", "api", "python", "-", "--timeout", str(timeout), "--output", "-",
                *(("--allow-live",) if allow_live else ()), stdin=program, timeout=timeout + 15,
            )
        try:
            report = json.loads(result.stdout)
            row_names = {"balance", "positions", "config", "pending_orders",
                         "orders_history", "fills_history", "pending_algo_orders"}
            flags = {"demo", "proxy_configured", "private_ws_connected", "private_ws_authenticated",
                     "algo_ws_connected", "algo_ws_authenticated", "order_lifecycle_verified", "trading_performed"}
            if not isinstance(report, dict) or set(report) != flags | {"checked_at", "scope", "rows"}:
                raise ValueError("unexpected report fields")
            if any(type(report[key]) is not bool for key in flags):
                raise ValueError("invalid flags")
            if report["scope"] != "private_rest_and_websocket_read_only" or report["trading_performed"] or report["order_lifecycle_verified"]:
                raise ValueError("invalid scope")
            if not report["demo"] and not allow_live:
                raise ValueError("live probe not approved")
            if not all(report[key] for key in flags if key.startswith(("private_ws_", "algo_ws_"))):
                raise ValueError("private streams not ready")
            if not isinstance(report["rows"], dict) or set(report["rows"]) != row_names:
                raise ValueError("invalid row keys")
            if any(type(count) is not int or count < 0 for count in report["rows"].values()):
                raise ValueError("invalid row counts")
            checked = datetime.fromisoformat(report["checked_at"])
            if checked.tzinfo is None or not started <= checked <= datetime.now(timezone.utc):
                raise ValueError("stale report")
        except (ValueError, TypeError, KeyError) as error:
            raise DeploymentError("Private smoke returned invalid or incomplete evidence; no report was published.") from error
        output.parent.mkdir(parents=True, exist_ok=True)
        write_json(output, report)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return report

    def proxy_smoke(self, *, timeout: float = 45, symbols: str = "BTC-USDT-SWAP,ETH-USDT-SWAP") -> dict:
        if not 5 <= timeout <= 300:
            raise DeploymentError("Proxy smoke timeout must be between 5 and 300 seconds.")
        requested = [item.strip().upper() for item in symbols.split(",") if item.strip()]
        if not requested or any(not re.fullmatch(r"[A-Z0-9]+-[A-Z0-9]+-SWAP", item) for item in requested):
            raise DeploymentError("Proxy smoke symbols must contain OKX perpetual instruments.")
        output = self.root / "outputs/proxy-verification.json"
        output.unlink(missing_ok=True)
        self.require_environment_file()
        started = datetime.now(timezone.utc)
        with (self.root / "infra/proxy-smoke.py").open("rb") as program:
            result = self.compose(
                "exec", "-T", "api", "python", "-",
                "--timeout", str(timeout), "--symbols", ",".join(requested), "--output", "-",
                stdin=program, timeout=timeout + 15,
            )
        try:
            report = json.loads(result.stdout)
            fields = {
                "checked_at", "scope", "proxy_configured", "proxy_scheme", "rest",
                "websocket", "private_account_verified", "trading_performed", "elapsed_seconds",
            }
            if not isinstance(report, dict) or set(report) != fields:
                raise ValueError("unexpected report fields")
            if report["scope"] != "okx_public_read_only_through_outbound_proxy":
                raise ValueError("invalid scope")
            if report["proxy_configured"] is not True or report["proxy_scheme"] not in {"http", "https", "socks5", "socks5h"}:
                raise ValueError("proxy is not confirmed")
            if report["private_account_verified"] is not False or report["trading_performed"] is not False:
                raise ValueError("unsafe probe report")
            checked = datetime.fromisoformat(report["checked_at"])
            if checked.tzinfo is None or not started <= checked <= datetime.now(timezone.utc):
                raise ValueError("stale report")
            elapsed = report["elapsed_seconds"]
            if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
                raise ValueError("invalid elapsed time")

            rest = report["rest"]
            if not isinstance(rest, list) or len(rest) != len(requested):
                raise ValueError("invalid REST evidence")
            for symbol, row in zip(requested, rest):
                if not isinstance(row, dict) or set(row) != {"instrument", "ticker_instrument_matches", "candles_received"}:
                    raise ValueError("invalid REST evidence")
                if row["instrument"] != symbol or row["ticker_instrument_matches"] is not True:
                    raise ValueError("REST instrument mismatch")
                if type(row["candles_received"]) is not int or row["candles_received"] < 2:
                    raise ValueError("invalid candle count")

            websocket = report["websocket"]
            websocket_fields = {
                "quotes_connected", "quotes_fresh", "candles_connected", "candles_fresh",
                "symbols", "candle_bars", "quotes_advanced", "received",
            }
            if not isinstance(websocket, dict) or set(websocket) != websocket_fields:
                raise ValueError("invalid WebSocket evidence")
            if any(websocket[name] is not True for name in (
                "quotes_connected", "quotes_fresh", "candles_connected", "candles_fresh", "quotes_advanced",
            )):
                raise ValueError("WebSocket stream is not fresh")
            bars = ["1m", "15m", "1H", "4H"]
            if websocket["symbols"] != requested or websocket["candle_bars"] != bars:
                raise ValueError("WebSocket subscriptions do not match request")
            received = websocket["received"]
            if not isinstance(received, list) or len(received) != len(requested):
                raise ValueError("incomplete WebSocket evidence")
            for symbol, row in zip(requested, received):
                if not isinstance(row, dict) or set(row) != {"instrument", "quote_fresh", "fresh_candle_bars"}:
                    raise ValueError("invalid WebSocket evidence")
                if row["instrument"] != symbol or row["quote_fresh"] is not True or row["fresh_candle_bars"] != bars:
                    raise ValueError("stale or incomplete WebSocket evidence")
        except (ValueError, TypeError, KeyError) as error:
            raise DeploymentError("Proxy smoke returned invalid or incomplete evidence; no report was published.") from error
        output.parent.mkdir(parents=True, exist_ok=True)
        write_json(output, report)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return report

    def ai_live_smoke(
        self, *, timeout: float = 240, inst_id: str = "BTC-USDT-SWAP",
        bar: str = "15m", limit: int = 100, run_mode: str | None = None,
    ) -> dict:
        if not 30 <= timeout <= 1800:
            raise DeploymentError("AI live smoke timeout must be between 30 and 1800 seconds.")
        if not re.fullmatch(r"[A-Z0-9]{2,20}-(?:USDT|USDC|USD)-SWAP", inst_id):
            raise DeploymentError("AI live smoke instrument must be an OKX perpetual instrument.")
        if not re.fullmatch(r"[0-9]+[mHhDWMw]", bar) or not 30 <= limit <= 300:
            raise DeploymentError("AI live smoke candle parameters are invalid.")
        if run_mode is not None and run_mode not in {"fast", "full"}:
            raise DeploymentError("AI live smoke run mode must be fast or full.")
        output = self.root / "outputs/ai-verification.json"
        output.unlink(missing_ok=True)
        self.require_environment_file()
        started = datetime.now(timezone.utc)
        with (self.root / "infra/ai-live-smoke.py").open("rb") as program:
            probe_args = [
                "--timeout", str(timeout), "--inst-id", inst_id,
                "--bar", bar, "--limit", str(limit), "--output", "-",
            ]
            if run_mode is not None:
                probe_args.extend(["--run-mode", run_mode])
            result = self.compose(
                "exec", "-T", "api", "python", "-",
                *probe_args,
                stdin=program, timeout=timeout + 15,
            )
        try:
            report = json.loads(result.stdout)
            fields = {
                "checked_at", "scope", "instrument", "bar", "mode", "provider",
                "market_evidence", "analysis", "provider_connection_verified",
                "execution_authorized", "private_account_verified", "trading_performed",
                "elapsed_seconds",
            }
            if not isinstance(report, dict) or set(report) != fields:
                raise ValueError("unexpected report fields")
            if report["scope"] != "tradingagents_real_model_read_only":
                raise ValueError("invalid scope")
            if report["instrument"] != inst_id or report["bar"] != bar:
                raise ValueError("instrument or bar mismatch")
            if run_mode is not None and report.get("mode") != run_mode:
                raise ValueError("run mode mismatch")
            if report["provider_connection_verified"] is not True:
                raise ValueError("model provider was not verified")
            if report["execution_authorized"] is not False or report["private_account_verified"] is not False or report["trading_performed"] is not False:
                raise ValueError("unsafe AI report")
            checked = datetime.fromisoformat(report["checked_at"])
            if checked.tzinfo is None or not started <= checked <= datetime.now(timezone.utc):
                raise ValueError("stale report")
            elapsed = report["elapsed_seconds"]
            if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
                raise ValueError("invalid elapsed time")
            evidence = report["market_evidence"]
            if not isinstance(evidence, dict) or evidence.get("ticker_received") is not True or evidence.get("funding_rate_received") is not True or evidence.get("open_interest_received") is not True:
                raise ValueError("incomplete market evidence")
            if type(evidence.get("candles_received")) is not int or evidence["candles_received"] < 30 or evidence.get("errors") != []:
                raise ValueError("incomplete candle evidence")
            analysis = report["analysis"]
            if not isinstance(analysis, dict) or analysis.get("decision_nonempty") is not True or not isinstance(analysis.get("state_keys"), list) or not analysis["state_keys"]:
                raise ValueError("empty AI result")
        except (ValueError, TypeError, KeyError) as error:
            raise DeploymentError("AI live smoke returned invalid or incomplete evidence; no report was published.") from error
        output.parent.mkdir(parents=True, exist_ok=True)
        write_json(output, report)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return report

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
    private_smoke = commands.add_parser("private-smoke")
    private_smoke.add_argument("--timeout", type=float, default=45)
    private_smoke.add_argument("--allow-live", action="store_true")
    proxy_smoke = commands.add_parser("proxy-smoke")
    proxy_smoke.add_argument("--timeout", type=float, default=45)
    proxy_smoke.add_argument("--symbols", default="BTC-USDT-SWAP,ETH-USDT-SWAP")
    ai_live_smoke = commands.add_parser("ai-live-smoke")
    ai_live_smoke.add_argument("--timeout", type=float, default=240)
    ai_live_smoke.add_argument("--inst-id", default="BTC-USDT-SWAP")
    ai_live_smoke.add_argument("--bar", default="15m")
    ai_live_smoke.add_argument("--limit", type=int, default=100)
    ai_live_smoke.add_argument("--run-mode", choices=("fast", "full"), default=None)
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
            if args.command == "private-smoke":
                deployment.private_smoke(timeout=args.timeout, allow_live=args.allow_live)
            elif args.command == "proxy-smoke":
                deployment.proxy_smoke(timeout=args.timeout, symbols=args.symbols)
            elif args.command == "ai-live-smoke":
                deployment.ai_live_smoke(
                    timeout=args.timeout, inst_id=args.inst_id,
                    bar=args.bar, limit=args.limit, run_mode=args.run_mode,
                )
            elif args.command == "backup":
                deployment.backup()
            elif args.command == "restore":
                deployment.restore(args.source.absolute())
            elif args.command == "down":
                deployment.compose("down", stdout=None)
            else:
                environment = deployment.preflight()
                arguments = ["up", "-d"]
                if environment.get("OPENPERPDESK_USE_PREBUILT_IMAGES", "false").lower() != "true":
                    arguments.append("--build")
                arguments.extend(["--wait", "--wait-timeout", "90"])
                deployment.compose(*arguments, stdout=None, timeout=None)
                deployment.compose("exec", "-T", "web", "nginx", "-s", "reload")


if __name__ == "__main__":
    try:
        main()
    except (DeploymentError, MaintenanceError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
