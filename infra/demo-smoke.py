"""Exercise the real API against isolated loopback exchange and PushPlus sinks."""

import argparse
import json
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AcceptanceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def startTest(self, test):
        super().startTest(test)
        self.started = time.monotonic()

    def stopTest(self, test):
        failures = {str(item) for item, _ in [*self.failures, *self.errors]}
        skipped = {str(item) for item, _ in self.skipped}
        self.records.append({
            "test": test.id(),
            "status": "failed" if str(test) in failures else "skipped" if str(test) in skipped else "passed",
            "seconds": round(time.monotonic() - self.started, 3),
        })
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(
        description="Local-only trading-flow acceptance. No real OKX account or WeChat delivery is used.",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/trading-flow-verification.json")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "services/api"))
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_trading_flow")
    result = unittest.TextTestRunner(verbosity=2, resultclass=AcceptanceResult).run(suite)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "environment": "isolated API subprocess, temporary SQLite, loopback REST/WebSocket/PushPlus",
        "real_okx_demo_verified": False,
        "real_pushplus_delivery_verified": False,
        "passed": result.wasSuccessful() and not result.skipped,
        "tests": result.records,
        "coverage": [
            "administrator access and direct-order rejection",
            "structured analysis and exchange-backed preview",
            "server account figures override client input",
            "account outage and excessive notional fail before leverage or submission",
            "signed Demo-only submission with attached mark-price TP/SL",
            "idempotent replay without another submission",
            "first-sync position protection and order/fill reconciliation",
            "native protection event, close fill, and position closure",
            "notification deduplication across sync and restart",
            "persistent emergency stop and independent live lock",
            "standard and native-protection cancellation acknowledgment versus final state",
            "API process killed after exchange acceptance before response",
            "restart recovery through exact client-order lookup, without resending",
            "worker dry run, sized execution, existing-position skip, protective reduce-only close",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Acceptance report: {args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
