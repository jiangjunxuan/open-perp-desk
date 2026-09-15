"""Read-only OKX private REST/WebSocket acceptance probe.

This probe never imports the order execution client and never sends a trading
request. It reports only connection state and row counts, not account values.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))

from app.okx_account import OkxAccountClient  # noqa: E402
from app.okx_account_stream import OkxAccountStream  # noqa: E402
from app.okx_algo_stream import OkxAlgoOrderStream  # noqa: E402


async def wait_for_authentication(
    account_stream: OkxAccountStream,
    algo_stream: OkxAlgoOrderStream,
    timeout: float,
) -> None:
    async with asyncio.timeout(timeout):
        while not (account_stream.authenticated and algo_stream.authenticated):
            errors = [error for error in (
                account_stream.last_error,
                algo_stream.last_error,
            ) if error]
            if errors and all(error in {"OkxAuthenticationError", "login_failed"} for error in errors):
                raise RuntimeError("Private WebSocket authentication was rejected.")
            await asyncio.sleep(0.1)


async def run_probe(*, timeout: float, allow_live: bool, output: Path) -> dict:
    account = OkxAccountClient()
    account_stream = OkxAccountStream()
    algo_stream = OkxAlgoOrderStream()
    if not account.configured:
        raise RuntimeError("OKX private credentials are not configured.")
    if not account.demo and not allow_live:
        raise RuntimeError("Refusing a live private probe without --allow-live.")

    await asyncio.gather(account_stream.start(), algo_stream.start())
    try:
        await wait_for_authentication(account_stream, algo_stream, timeout)
        balance, positions, config, pending, orders, fills, algo_orders = await asyncio.gather(
            account.balance(),
            account.positions(),
            account.config(),
            account.pending_orders(),
            account.orders_history(limit=100),
            account.fills_history(limit=100),
            account.pending_algo_orders(limit=100),
        )
        result = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "scope": "private_rest_and_websocket_read_only",
            "demo": account.demo,
            "proxy_configured": bool(os.getenv("OKX_PROXY_URL", "").strip()),
            "private_ws_authenticated": account_stream.authenticated,
            "algo_ws_authenticated": algo_stream.authenticated,
            "rows": {
                "balance": len(balance),
                "positions": len(positions),
                "config": len(config),
                "pending_orders": len(pending),
                "orders_history": len(orders),
                "fills_history": len(fills),
                "pending_algo_orders": len(algo_orders),
            },
            "trading_performed": False,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
    finally:
        await asyncio.gather(account_stream.stop(), algo_stream.stop())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "outputs/okx-private-verification.json",
    )
    args = parser.parse_args()
    if not 5 <= args.timeout <= 300:
        parser.error("--timeout must be between 5 and 300 seconds")
    try:
        result = asyncio.run(run_probe(
            timeout=args.timeout,
            allow_live=args.allow_live,
            output=args.output,
        ))
    except (RuntimeError, TimeoutError, OSError) as error:
        print(f"Private OKX smoke failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
