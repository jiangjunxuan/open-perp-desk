"""Read-only SSE smoke check for the complete reverse-proxy path."""

import argparse
import json
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def observe(base_url, route, target):
    started = time.monotonic()
    arrivals = []
    event = ""
    data = []
    request = Request(base_url + route, headers={"Accept": "text/event-stream"})
    with urlopen(request, timeout=8) as response:
        if response.status != 200 or response.headers.get_content_type() != "text/event-stream":
            raise RuntimeError(f"{route}: response is not an SSE stream")
        while len(arrivals) < 2:
            if time.monotonic() - started > 12:
                raise RuntimeError(f"{route}: expected events did not arrive")
            line = response.readline(2**21)
            if not line or len(line) >= 2**21:
                raise RuntimeError(f"{route}: stream closed or frame exceeded limit")
            line = line.decode("utf-8").rstrip("\r\n")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line:
                if data:
                    json.loads("\n".join(data))
                    if event == target:
                        arrivals.append(time.monotonic() - started)
                event, data = "", []
    if arrivals[0] > 4 or arrivals[1] - arrivals[0] > 7:
        raise RuntimeError(f"{route}: events are buffered or delayed")
    return {
        "route": route,
        "event": target,
        "first_event_seconds": round(arrivals[0], 3),
        "next_event_seconds": round(arrivals[1] - arrivals[0], 3),
    }


def verify_private_lock(base_url):
    try:
        with urlopen(base_url + "/api/v1/account/events", timeout=8) as response:
            raise RuntimeError(f"Unauthenticated private stream returned HTTP {response.status}")
    except HTTPError as error:
        try:
            if error.code not in {401, 503}:
                raise RuntimeError(f"Unexpected private access status: {error.code}") from error
            return error.code
        finally:
            error.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("Use an HTTP(S) origin without credentials, query or fragment.")
    checks = [
        observe(base_url, "/api/v1/system/events", "heartbeat"),
        observe(base_url, "/api/v1/market/events?bar=15m", "market"),
    ]
    print(json.dumps({
        "checks": checks,
        "private_unauthenticated_status": verify_private_lock(base_url),
        "read_only": True,
    }, indent=2))


if __name__ == "__main__":
    main()
