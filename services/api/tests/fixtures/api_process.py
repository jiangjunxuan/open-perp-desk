"""Actual API subprocess with a temporary state directory and loopback sinks."""

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[4]
ADMIN_TOKEN = "local-acceptance-admin-token"


async def eventually(predicate, timeout=15):
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(.05)


class ApiProcess:
    def __init__(self, directory, exchange):
        self.directory = Path(directory)
        self.exchange = exchange
        self.database = self.directory / "isolated.sqlite3"
        self.process = None
        self.log = None
        self.client = None
        self.environment = {
            "PATH": os.defpath, "HOME": str(self.directory), "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(ROOT / "services/api"), "APP_ENV": "test",
            "ADMIN_API_TOKEN": ADMIN_TOKEN, "STATE_DB_PATH": str(self.database),
            "EXECUTION_ENABLED": "true", "OKX_DEMO": "true", "TRADING_MODE": "demo",
            "LIVE_TRADING_ENABLED": "false", "AUTO_TRADING_ENABLED": "false",
            "AUTO_TRADING_DRY_RUN": "true", "ACCOUNT_SYNC_INTERVAL_SECONDS": "3600",
            "AUTO_TRADING_INTERVAL_SECONDS": "15", "MARKET_SYMBOLS": "BTC-USDT-SWAP",
            "AUTO_TRADING_SYMBOLS": "BTC-USDT-SWAP", "TRADINGAGENTS_ENABLED": "false",
            "OKX_PROXY_URL": "", "PUSHPLUS_PROXY_URL": "", "NO_PROXY": "*",
            **exchange.environment(),
        }
        for name, value in self.environment.items():
            if name.endswith(("BASE_URL", "_URL")) and value:
                if urlsplit(value).hostname != "127.0.0.1":
                    raise ValueError(f"Refusing a non-loopback acceptance endpoint: {name}")

    async def start(self):
        self.log = (self.directory / "api.log").open("ab")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            descriptor = listener.fileno()
            self.origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
            self.process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--fd", str(descriptor),
                 "--log-level", "warning", "--no-access-log"],
                cwd=ROOT / "services/api", env=self.environment, pass_fds=(descriptor,),
                stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
            )
        self.client = httpx.AsyncClient(
            base_url=self.origin, headers={"X-Admin-Token": ADMIN_TOKEN},
            timeout=20, trust_env=False,
        )

        async def ready():
            if self.process.poll() is not None:
                raise RuntimeError(f"API subprocess exited: {(self.directory / 'api.log').read_text()[-3000:]}")
            try:
                response = await self.client.get("/api/v1/health/metrics")
                if response.status_code != 200:
                    return False
                result = response.json()
                return (
                    result["market_stream"]["fresh"]
                    and result["account_stream"]["authenticated"]
                    and result["algo_stream"]["authenticated"]
                    and result["account_reconciler"]["last_sync_at"] is not None
                )
            except httpx.HTTPError:
                return False

        await eventually(ready)
        return self

    async def request(self, method, route, payload=None, *, status=200):
        response = await self.client.request(method, "/api/v1" + route, json=payload)
        if response.status_code != status:
            raise AssertionError(f"{method} {route}: HTTP {response.status_code}: {response.text}")
        return response.json()

    async def stop(self, *, crash=False):
        if self.process and self.process.poll() is None:
            self.process.kill() if crash else self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, timeout=5)
        if self.client:
            await self.client.aclose()
            self.client = None
        if self.log:
            self.log.close()
            self.log = None
