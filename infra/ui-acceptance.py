"""Run the browser regression against isolated loopback exchange/API processes."""

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))

from tests.fixtures.api_process import ApiProcess
from tests.fixtures.exchange_server import ExchangeServer


async def main():
    artifacts = ROOT / "work/ci-ui-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="openperpdesk-ui-acceptance-") as directory:
        exchange = ExchangeServer()
        api = None
        browser = None
        try:
            await exchange.start()
            api = ApiProcess(directory, exchange)
            api.environment.update({
                "EXECUTION_ENABLED": "false",
                "MARKET_SYMBOLS": "BTC-USDT-SWAP,ETH-USDT-SWAP",
            })
            await api.start()
            page = await api.client.get("/")
            if page.status_code != 200 or "price-chart" not in page.text:
                raise RuntimeError("Isolated API did not serve the trading console")
            environment = {
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": os.environ.get("HOME", directory),
                "OPENPERPDESK_ORIGIN": api.origin,
                "OPENPERPDESK_OUTPUT_DIR": str(artifacts),
                "OPENPERPDESK_DISABLE_PROXY": "true",
            }
            if os.getenv("CHROME_BIN"):
                environment["CHROME_BIN"] = os.environ["CHROME_BIN"]
            with (artifacts / "browser.log").open("wb") as log:
                browser = await asyncio.create_subprocess_exec(
                    "node", str(ROOT / "infra/ui-smoke.mjs"), cwd=ROOT,
                    env=environment, stdout=log, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                code = await asyncio.wait_for(browser.wait(), 480)
            if code:
                raise RuntimeError((artifacts / "browser.log").read_text()[-6000:])
            status = await api.request("GET", "/system/status")
            assert not status["execution_enabled"]
            assert not status["automation_worker"]["enabled"]
            assert not status["live_safety"]["allowed"]
            assert not exchange.posts, "Browser acceptance unexpectedly sent exchange mutations"
            assert not exchange.errors, exchange.errors
            report = json.loads((artifacts / "ui-verification.json").read_text())
            summary = {
                "scope": "loopback protocol fixtures, not real OKX acceptance",
                "routes": sum(len(row["routeChecks"]) for row in report["results"]),
                "realtime_viewports": len(report["realtime"]),
                "annotation_groups": len(report["annotations"]),
                "protection_incident_groups": len(report["protectionIncidents"]),
                "browser_errors": report["browserErrors"],
                "exchange_mutations": len(exchange.posts),
                "execution_enabled": False,
            }
            (artifacts / "acceptance-summary.json").write_text(json.dumps(summary, indent=2))
            print(json.dumps(summary, indent=2))
        finally:
            if browser is not None and browser.returncode is None:
                os.killpg(browser.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(browser.wait(), 5)
                except TimeoutError:
                    os.killpg(browser.pid, signal.SIGKILL)
                    await browser.wait()
            if api is not None:
                await api.stop()
                log = Path(directory) / "api.log"
                if log.exists():
                    shutil.copyfile(log, artifacts / "api.log")
            await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
