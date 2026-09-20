import asyncio
import fcntl
import json
import os
import re
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any


ERRORS = {
    "disabled": (503, "TradingAgents is disabled."),
    "source_missing": (503, "TradingAgents source tree is missing or incomplete."),
    "runtime_missing": (503, "TradingAgents Python runtime is unavailable."),
    "invalid_config": (503, "TradingAgents configuration is invalid."),
    "dependencies_missing": (503, "TradingAgents dependencies are not installed in its Python runtime."),
    "provider_configuration": (503, "TradingAgents provider or model configuration is not ready."),
    "provider_unavailable": (503, "TradingAgents model provider is temporarily unavailable. Retry later."),
    "busy": (409, "Another TradingAgents operation is running. Try again after it finishes."),
    "timeout": (504, "TradingAgents exceeded its time limit and was stopped."),
    "output_limit": (502, "TradingAgents output exceeded the configured size limit."),
    "request_limit": (422, "TradingAgents input exceeded the configured size limit."),
    "invalid_instrument": (422, "AI research requires an OKX perpetual instrument identifier."),
    "runtime_failed": (502, "TradingAgents research failed. Check provider access and model support."),
    "invalid_result": (502, "TradingAgents returned an invalid result."),
    "storage_unavailable": (503, "TradingAgents storage is unavailable."),
}

# Never forward the API process environment wholesale to a research process.
PROVIDER_ENV = {
    "OPENAI_API_KEY", "OPENAI_COMPATIBLE_API_KEY", "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
    "OPENAI_API_VERSION", "XAI_API_KEY", "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY", "DASHSCOPE_CN_API_KEY", "ZHIPU_API_KEY", "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY", "MINIMAX_CN_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY",
    "MOONSHOT_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY", "OLLAMA_BASE_URL",
    "FRED_API_KEY", "ALPHA_VANTAGE_API_KEY",
}
SYSTEM_ENV = {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"}
CONFIG_ENV = {
    "TRADINGAGENTS_LLM_PROVIDER": "llm_provider",
    "TRADINGAGENTS_DEEP_THINK_LLM": "deep_think_llm",
    "TRADINGAGENTS_QUICK_THINK_LLM": "quick_think_llm",
    "TRADINGAGENTS_LLM_BACKEND_URL": "backend_url",
    "TRADINGAGENTS_OUTPUT_LANGUAGE": "output_language",
    "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "openai_reasoning_effort",
    "TRADINGAGENTS_GOOGLE_THINKING_LEVEL": "google_thinking_level",
    "TRADINGAGENTS_ANTHROPIC_EFFORT": "anthropic_effort",
}


class AIAnalysisError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if code in ERRORS else "runtime_failed"
        self.status_code, message = ERRORS[self.code]
        super().__init__(message)


class TradingAgentsAdapter:
    """Bounded optional research process, with no trading environment or API imports."""

    def __init__(self) -> None:
        self.enabled = os.getenv("TRADINGAGENTS_ENABLED", "false").lower() == "true"
        self.path = os.getenv("TRADINGAGENTS_PATH", "").strip()
        # A venv's executable path selects its packages; do not resolve its symlink.
        self.python = os.path.abspath(os.getenv("TRADINGAGENTS_PYTHON", "").strip() or sys.executable)
        self.root = Path(os.getenv("DATA_DIR", "data")).absolute() / "tradingagents"
        self.runtime_state = "unchecked"
        self.last_error: str | None = None
        self._tasks: set[asyncio.Task] = set()
        self._configuration_error = False
        self.run_mode = os.getenv("TRADINGAGENTS_RUN_MODE", "full").strip().lower() or "full"
        if self.run_mode not in {"fast", "full"}:
            self._configuration_error = True
            self.run_mode = "full"
        self.config: dict[str, Any] = {"output_language": "Chinese", "checkpoint_enabled": False}
        for name, key in CONFIG_ENV.items():
            value = os.getenv(name, "").strip()
            if value:
                self.config[key] = value
        for name, key, default, lower, upper in (
            ("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "max_debate_rounds", 1, 1, 5),
            ("TRADINGAGENTS_MAX_RISK_ROUNDS", "max_risk_discuss_rounds", 1, 1, 5),
            ("TRADINGAGENTS_LLM_MAX_RETRIES", "llm_max_retries", 1, 0, 3),
            ("TRADINGAGENTS_MAX_TOKENS", "max_tokens", 4096, 128, 32768),
            ("TRADINGAGENTS_MAX_RECUR_LIMIT", "max_recur_limit", 80, 10, 200),
            ("TRADINGAGENTS_TIMEOUT_SECONDS", "_timeout", 300, 1, 1800),
            ("TRADINGAGENTS_MAX_OUTPUT_BYTES", "_output_limit", 4 * 1024 * 1024, 1024, 16 * 1024 * 1024),
        ):
            try:
                value = int(os.getenv(name, str(default)))
                if not lower <= value <= upper:
                    raise ValueError()
            except ValueError:
                self._configuration_error = True
                value = default
            self.config[key] = value
        self.timeout_seconds = self.config.pop("_timeout")
        self.output_limit = self.config.pop("_output_limit")
        try:
            self.data_timeout_seconds = int(os.getenv("TRADINGAGENTS_DATA_TIMEOUT_SECONDS", "8"))
            if not 1 <= self.data_timeout_seconds <= 60:
                raise ValueError()
        except ValueError:
            self._configuration_error = True
            self.data_timeout_seconds = 8
        for name, key, relative in (
            ("TRADINGAGENTS_RESULTS_DIR", "results_dir", "results"),
            ("TRADINGAGENTS_CACHE_DIR", "data_cache_dir", "cache"),
            ("TRADINGAGENTS_MEMORY_LOG_PATH", "memory_log_path", "memory/trading_memory.md"),
        ):
            self.config[key] = str(Path(os.getenv(name, "").strip() or self.root / relative).absolute())
        self.environment = {
            name: value for name, value in os.environ.items()
            if name in PROVIDER_ENV | SYSTEM_ENV and value
        }

    @property
    def configuration_error(self) -> str | None:
        if not self.enabled:
            return "disabled"
        if self._configuration_error:
            return "invalid_config"
        if not self.path or not all(
            (Path(self.path) / relative).is_file()
            for relative in ("tradingagents/default_config.py", "tradingagents/graph/trading_graph.py")
        ):
            return "source_missing"
        if not Path(self.python).is_file() or not os.access(self.python, os.X_OK):
            return "runtime_missing"
        return None

    @property
    def configured(self) -> bool:
        return self.configuration_error is None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "configured": self.configured,
            "runtime_state": self.runtime_state, "busy": bool(self._tasks),
            "last_error": self.configuration_error or self.last_error,
            "run_mode": self.run_mode,
            "available_run_modes": ["fast", "full"],
            "timeout_seconds": self.timeout_seconds,
            "data_timeout_seconds": self.data_timeout_seconds,
            "execution_authorized": False,
        }

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        # Include tool subprocesses that may still hold result pipes open.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 1)
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Drain paused stdout after an output-limit failure so its transport closes.
        await process.communicate()

    async def _invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        error = self.configuration_error
        if error:
            raise AIAnalysisError(error)
        try:
            payload = json.dumps({
                **request, "source_path": str(Path(self.path).absolute()), "config": self.config,
                "run_mode": request.get("run_mode") or self.run_mode,
                "parent_pid": os.getpid(), "timeout_seconds": self.timeout_seconds,
                "data_timeout_seconds": self.data_timeout_seconds,
            }, ensure_ascii=True, allow_nan=False).encode()
        except (TypeError, ValueError):
            raise AIAnalysisError("invalid_result") from None
        if len(payload) > 256 * 1024:
            raise AIAnalysisError("request_limit")
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = (self.root / ".research.lock").open("a+b")
        except OSError:
            raise AIAnalysisError("storage_unavailable") from None
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AIAnalysisError("busy") from None
            task = asyncio.current_task()
            self._tasks.add(task)
            process = None
            workspace = None
            try:
                workspace = tempfile.TemporaryDirectory(prefix="run-", dir=self.root)
                environment = {
                    **self.environment,
                    "HOME": workspace.name, "TMPDIR": workspace.name, "XDG_CACHE_HOME": workspace.name,
                    "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
                }
                spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                    self.python, "-I", str(Path(__file__).with_name("ai_runner.py")),
                    cwd=workspace.name, env=environment, start_new_session=True,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                ))
                try:
                    process = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    process = await spawn
                    raise

                async def exchange() -> bytes:
                    process.stdin.write(payload)
                    await process.stdin.drain()
                    process.stdin.close()
                    chunks = []
                    size = 0
                    while chunk := await process.stdout.read(65536):
                        size += len(chunk)
                        if size > self.output_limit:
                            raise AIAnalysisError("output_limit")
                        chunks.append(chunk)
                    await process.wait()
                    if process.returncode:
                        raise AIAnalysisError("runtime_failed")
                    return b"".join(chunks)

                timeout = min(self.timeout_seconds, 30) if request["action"] == "probe" else self.timeout_seconds
                try:
                    output = await asyncio.wait_for(exchange(), timeout)
                except asyncio.TimeoutError:
                    raise AIAnalysisError("timeout") from None
                try:
                    result = json.loads(output)
                except (ValueError, UnicodeDecodeError):
                    raise AIAnalysisError("invalid_result") from None
                if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                    raise AIAnalysisError("invalid_result")
                if not result["ok"]:
                    code = result.get("code")
                    raise AIAnalysisError(code if isinstance(code, str) else "runtime_failed")
                data = result.get("data")
                if not isinstance(data, dict):
                    raise AIAnalysisError("invalid_result")
                self.runtime_state = "ready"
                self.last_error = None
                return data
            except AIAnalysisError as exc:
                self.last_error = exc.code
                self.runtime_state = "failed"
                raise
            except asyncio.CancelledError:
                self.runtime_state = "canceled"
                raise
            except OSError:
                self.last_error = "runtime_failed"
                self.runtime_state = "failed"
                raise AIAnalysisError("runtime_failed") from None
            finally:
                try:
                    if process is not None:
                        cleanup = asyncio.create_task(self._terminate(process))
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            await cleanup
                            raise
                finally:
                    self._tasks.discard(task)
                    if workspace is not None:
                        workspace.cleanup()
        finally:
            lock.close()

    async def check_ready(self) -> dict[str, Any]:
        return await self._invoke({"action": "probe"})

    async def analyze(
        self, inst_id: str, market_context: dict[str, Any] | None = None,
        run_mode: str | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Z0-9]{2,20}-(?:USDT|USDC|USD)-SWAP", inst_id):
            raise AIAnalysisError("invalid_instrument")
        selected_mode = str(run_mode or self.run_mode).strip().lower()
        if selected_mode not in {"fast", "full"}:
            raise AIAnalysisError("invalid_config")
        data = await self._invoke({
            "action": "analyze", "inst_id": inst_id,
            "market_context": market_context or {}, "run_mode": selected_mode,
        })
        if data.get("inst_id") != inst_id or not isinstance(data.get("state"), dict) or "decision" not in data:
            self.last_error = "invalid_result"
            self.runtime_state = "failed"
            raise AIAnalysisError("invalid_result")
        data.update(source="TradingAgents", bias="research", signal={}, execution_authorized=False)
        data["mode"] = selected_mode
        return data
