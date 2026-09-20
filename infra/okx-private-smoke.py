"""Read-only OKX private REST/WebSocket acceptance probe.

This probe never imports the order execution client and never sends a trading
request. It reports only connection state and row counts, not account values.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] if __file__ != "<stdin>" else Path.cwd()
sys.path.insert(0, str(ROOT / "services/api"))

from app.database_maintenance import write_json  # noqa: E402
from app.okx_account import OkxAccountClient  # noqa: E402
from app.okx_account_stream import OkxAccountStream  # noqa: E402
from app.okx_algo_stream import OkxAlgoOrderStream  # noqa: E402
from app.private_connection import PrivateProbeError, probe_connections  # noqa: E402


async def run_probe(*, timeout: float, allow_live: bool, output: Path | None) -> dict:
    if output is not None:
        output.unlink(missing_ok=True)
    result = await probe_connections(
        OkxAccountClient(), OkxAccountStream(), OkxAlgoOrderStream(),
        timeout=timeout, allow_live=allow_live,
    )
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        write_json(output, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "outputs/okx-private-verification.json",
        help="JSON report path; use - for stdout only.",
    )
    args = parser.parse_args()
    if not 5 <= args.timeout <= 300:
        parser.error("--timeout must be between 5 and 300 seconds")
    try:
        result = asyncio.run(run_probe(
            timeout=args.timeout,
            allow_live=args.allow_live,
            output=None if args.output == Path("-") else args.output,
        ))
    except PrivateProbeError as error:
        print(f"Private OKX smoke failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except TimeoutError:
        print("Private OKX smoke failed: probe deadline exceeded.", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as error:
        # Provider messages and exceptions may reflect keys or proxy credentials.
        print(f"Private OKX smoke failed ({type(error).__name__}); no report was published.", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
