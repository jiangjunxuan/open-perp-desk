"""Verify one minimum-size OKX Demo order lifecycle through the public API.

This is an explicit trading acceptance probe. It refuses live mode, requires an
empty starting instrument, uses the normal signal/risk/preflight path, closes
the position, clears the attached protection order, and activates emergency
stop before publishing a success report.
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


CONFIRMATION = "OPENPERPDESK_OKX_DEMO_LIFECYCLE"
SCOPE = "okx_demo_order_lifecycle"
CLEANUP_TIMEOUT_SECONDS = 60.0
LOCK_TIMEOUT_SECONDS = 20.0
EXISTING_CLOSE_WAIT_SECONDS = 10.0
ACTIVE_ORDER_STATUSES = {
    "preparing", "submitting", "submitted", "pending", "accepted", "live",
    "partially_filled", "partially_effective", "submission_unknown", "unknown",
    "cancel_failed", "canceling",
}
TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "failed", "rejected", "effective",
    "triggered", "order_failed", "expired", "mmp_canceled",
}


class DemoLifecycleError(RuntimeError):
    """A fixed, non-secret lifecycle acceptance failure."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def boolean_environment(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value not in {"true", "false"}:
        raise DemoLifecycleError(f"{name} must be explicitly true or false.")
    return value == "true"


def validate_environment() -> None:
    if os.getenv("TRADING_MODE", "").strip().lower() != "demo":
        raise DemoLifecycleError("Demo lifecycle smoke refuses non-demo trading mode.")
    if not boolean_environment("OKX_DEMO"):
        raise DemoLifecycleError("Demo lifecycle smoke requires OKX_DEMO=true.")
    if not boolean_environment("EXECUTION_ENABLED"):
        raise DemoLifecycleError("Demo lifecycle smoke requires EXECUTION_ENABLED=true.")
    if boolean_environment("LIVE_TRADING_ENABLED"):
        raise DemoLifecycleError("Demo lifecycle smoke requires LIVE_TRADING_ENABLED=false.")
    if boolean_environment("AUTO_TRADING_ENABLED"):
        raise DemoLifecycleError("Disable the automation worker before Demo lifecycle smoke.")
    if not boolean_environment("AUTO_TRADING_DRY_RUN"):
        raise DemoLifecycleError("Demo lifecycle smoke requires AUTO_TRADING_DRY_RUN=true.")
    if os.getenv("TRADINGVIEW_ENABLED", "false").strip().lower() != "false":
        raise DemoLifecycleError("Disable TradingView before Demo lifecycle smoke.")
    if not all(os.getenv(name, "").strip() for name in (
        "ADMIN_API_TOKEN", "OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE",
    )):
        raise DemoLifecycleError("Administrator and OKX Demo credentials must be configured.")


def positive_decimal(value, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DemoLifecycleError(f"{label} is invalid.") from exc
    if not number.is_finite() or number <= 0:
        raise DemoLifecycleError(f"{label} is invalid.")
    return number


def number_value(value: Decimal) -> int | float:
    normalized = value.normalize()
    return int(normalized) if normalized == normalized.to_integral_value() else float(normalized)


def report_path(value: str) -> Path | None:
    if value == "-":
        return None
    return Path(value)


def publish(path: Path | None, report: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise DemoLifecycleError("Local API redirects are not allowed.")


class ApiClient:
    def __init__(self, origin: str, token: str) -> None:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise DemoLifecycleError("Demo lifecycle smoke only accepts a loopback HTTP origin.")
        self.origin = origin.rstrip("/")
        self.token = token
        self.deadline: float | None = None
        self.opener = build_opener(ProxyHandler({}), RejectRedirects())

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        remaining = 20.0 if self.deadline is None else self.deadline - time.monotonic()
        if remaining <= 0:
            raise DemoLifecycleError("The current acceptance phase timed out.")
        body = None if payload is None else json.dumps(
            payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
        ).encode()
        headers = {"Accept": "application/json", "X-Admin-Token": self.token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.origin}/api/v1{path}", data=body, headers=headers, method=method,
        )
        try:
            with self.opener.open(request, timeout=min(20.0, remaining)) as response:
                if response.status < 200 or response.status >= 300:
                    raise DemoLifecycleError(f"{method} {path} returned HTTP {response.status}.")
                raw = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as error:
            try:
                error.read(65536)
            finally:
                error.close()
            raise DemoLifecycleError(f"{method} {path} returned HTTP {error.code}.") from None
        except (URLError, OSError) as error:
            raise DemoLifecycleError(f"{method} {path} could not reach the local API.") from error
        if len(raw) > 2 * 1024 * 1024:
            raise DemoLifecycleError(f"{method} {path} returned an oversized response.")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as error:
            raise DemoLifecycleError(f"{method} {path} returned invalid JSON.") from error
        if not isinstance(result, dict):
            raise DemoLifecycleError(f"{method} {path} returned an invalid object.")
        return result

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def post(self, path: str, payload: dict | None = None) -> dict:
        return self.request("POST", path, payload or {})


def data_rows(payload: dict, label: str) -> list[dict]:
    rows = payload.get("data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise DemoLifecycleError(f"{label} returned invalid rows.")
    return rows


def active_orders(rows: list[dict], inst_id: str) -> list[dict]:
    return [
        row for row in rows
        if row.get("inst_id") == inst_id and row.get("status") in ACTIVE_ORDER_STATUSES
    ]


def active_close_orders(rows: list[dict], inst_id: str) -> list[dict]:
    return [
        row for row in active_orders(rows, inst_id)
        if row.get("reduce_only") and row.get("order_kind") != "algo"
    ]


def positions_for(rows: list[dict], inst_id: str) -> list[dict]:
    positions = []
    for row in rows:
        if row.get("inst_id") != inst_id:
            continue
        try:
            size = float(row["size"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise DemoLifecycleError("Position quantity could not be verified.") from None
        if not math.isfinite(size):
            raise DemoLifecycleError("Position quantity could not be verified.")
        if size:
            positions.append(row)
    return positions


def synchronized_snapshot(api: ApiClient) -> dict[str, list[dict]]:
    synced = api.post("/account/sync")
    rest = synced.get("rest")
    if not isinstance(rest, dict) or rest.get("errors"):
        raise DemoLifecycleError("Account reconciliation did not return a complete REST snapshot.")
    snapshot = {
        "positions": data_rows(api.get("/positions"), "Positions"),
        "orders": data_rows(api.get("/orders?limit=500"), "Orders"),
        "fills": data_rows(api.get("/fills?limit=500"), "Fills"),
    }
    if len(snapshot["orders"]) >= 500:
        raise DemoLifecycleError("Order history reached the snapshot limit; completeness is unverified.")
    return snapshot


def wait_for_snapshot(api: ApiClient, deadline: float, predicate, label: str) -> tuple[dict, object]:
    while time.monotonic() < deadline:
        try:
            snapshot = synchronized_snapshot(api)
            result = predicate(snapshot)
            if result:
                return snapshot, result
        except DemoLifecycleError:
            pass
        time.sleep(0.5)
    raise DemoLifecycleError(f"{label} was not verified before the deadline.")


def stream_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def stream_advanced(current: object, previous: object) -> bool:
    current_timestamp = stream_timestamp(current)
    previous_timestamp = stream_timestamp(previous)
    return current_timestamp is not None and (
        previous_timestamp is None or current_timestamp > previous_timestamp
    )


def private_filled_order(snapshot: dict, client_id: str) -> dict | None:
    orders = snapshot.get("orders")
    fills = snapshot.get("fills")
    if not isinstance(orders, list) or not isinstance(fills, list):
        return None
    order = next((
        row for row in orders
        if isinstance(row, dict)
        and row.get("clOrdId") == client_id
        and row.get("state") == "filled"
    ), None)
    if order is None:
        return None
    for fill in fills:
        if not isinstance(fill, dict) or fill.get("clOrdId") != client_id:
            continue
        if not str(fill.get("tradeId") or ""):
            continue
        try:
            positive_decimal(fill.get("fillSz"), "WebSocket fill quantity")
            positive_decimal(fill.get("fillPx"), "WebSocket fill price")
        except DemoLifecycleError:
            continue
        return order
    return None


def streamed_protection(api: ApiClient, opening: dict) -> str | None:
    attached = opening.get("attachAlgoOrds")
    if not isinstance(attached, list) or len(attached) != 1 or not isinstance(attached[0], dict):
        return None
    client_id = attached[0].get("attachAlgoClOrdId")
    if not isinstance(client_id, str) or not client_id:
        return None
    # The ledger preserves the first source, so REST cannot satisfy this check.
    for row in data_rows(api.get("/orders?limit=500"), "Orders"):
        if (
            row.get("client_order_id") == client_id
            and row.get("inst_id") == opening.get("instId")
            and row.get("order_kind") == "algo"
            and row.get("source") == "okx-algo-stream"
            and row.get("status") in ACTIVE_ORDER_STATUSES
        ):
            return client_id
    return None


def wait_for_private_fill(
    api: ApiClient, deadline: float, client_id: str, private_before: object,
    label: str, *, require_algo: bool = False, algo_before: object | None = None,
) -> dict:
    while time.monotonic() < deadline:
        try:
            account = api.get("/account/stream")
            status = api.get("/system/status")
            private_status = status.get("account_stream") or {}
            algo_status = status.get("algo_stream") or {}
            opening = private_filled_order(account, client_id)
            private_progress = stream_advanced(
                account.get("last_message_at") or private_status.get("last_message_at"),
                private_before,
            )
            algo_progress = not require_algo or (
                algo_status.get("connected") is True
                and algo_status.get("authenticated") is True
                and stream_advanced(algo_status.get("last_message_at"), algo_before)
            )
            protection_id = streamed_protection(api, opening) if require_algo and opening else None
            if (
                opening is not None
                and account.get("connected") is True
                and account.get("authenticated") is True
                and private_progress
                and algo_progress
                and (not require_algo or protection_id is not None)
            ):
                return {
                    "private_last_message_at": account.get("last_message_at")
                    or private_status.get("last_message_at"),
                    "algo_last_message_at": algo_status.get("last_message_at"),
                    "protection_client_id": protection_id,
                }
        except DemoLifecycleError:
            pass
        time.sleep(0.2)
    raise DemoLifecycleError(f"{label} was not verified on the private WebSocket before the deadline.")


def signal_payload(inst_id: str, action: str, *, source: str, position_pct: float,
                   entry: Decimal | None = None, stop: Decimal | None = None,
                   target: Decimal | None = None) -> dict:
    created = utc_now()
    signal = {
        "inst_id": inst_id,
        "action": action,
        "confidence": 1.0,
        "leverage": 1.0,
        "position_pct": position_pct,
        "source": source,
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(seconds=120)).isoformat(),
    }
    if entry is not None:
        signal.update(
            entry_price=float(entry), stop_loss=float(stop), take_profit=float(target),
        )
    return {
        "signal": signal,
        # Exchange-backed preflight replaces these compatibility values.
        "account_equity": 1.0,
        "daily_pnl_pct": 0.0,
        "current_notional": 0.0,
    }


def verify_initial_state(api: ApiClient, inst_id: str) -> tuple[dict, Decimal, Decimal]:
    status = api.get("/system/status")
    if status.get("trading_mode") != "demo" or status.get("execution_enabled") is not True:
        raise DemoLifecycleError("Local API is not an execution-enabled Demo instance.")
    if status.get("live_safety", {}).get("allowed") is not False:
        raise DemoLifecycleError("Live safety gate must remain locked during Demo acceptance.")
    tradingview = (status.get("integrations") or {}).get("tradingview") or {}
    if tradingview.get("enabled") is not False:
        raise DemoLifecycleError("TradingView must be disabled during Demo acceptance.")
    market = status.get("market_stream") or {}
    if market.get("connected") is not True or market.get("fresh") is not True:
        raise DemoLifecycleError("Public market stream is not fresh.")
    account_stream = status.get("account_stream") or {}
    algo_stream = status.get("algo_stream") or {}
    if not all((
        account_stream.get("configured") is True,
        account_stream.get("connected") is True,
        account_stream.get("authenticated") is True,
        account_stream.get("account_ready") is True,
        algo_stream.get("configured") is True,
        algo_stream.get("connected") is True,
        algo_stream.get("authenticated") is True,
    )):
        raise DemoLifecycleError("Private account and algo WebSocket streams are not ready.")
    worker = api.get("/worker/status")
    if worker.get("enabled") is not False or worker.get("running") is not False or worker.get("dry_run") is not True:
        raise DemoLifecycleError("Automation worker must be disabled and configured for Dry Run.")
    safety = api.get("/safety/status")
    if safety.get("emergency_stopped") is not False or safety.get("order_submission_allowed") is not True:
        raise DemoLifecycleError("Demo execution safety state is not ready for explicit acceptance.")
    overview = api.get("/account/overview")
    if overview.get("configured") is not True or overview.get("demo") is not True or overview.get("errors"):
        raise DemoLifecycleError("OKX Demo account snapshot is unavailable.")
    raw_positions = overview.get("positions")
    if not isinstance(raw_positions, list) or any(
        row.get("instId") == inst_id and abs(float(row.get("pos") or 0)) > 0
        for row in raw_positions if isinstance(row, dict)
    ):
        raise DemoLifecycleError("The acceptance instrument must start with no exchange position.")

    snapshot = synchronized_snapshot(api)
    if len(snapshot["orders"]) > 480:
        raise DemoLifecycleError("Use an isolated Demo account with room for new lifecycle records.")
    if positions_for(snapshot["positions"], inst_id) or active_orders(snapshot["orders"], inst_id):
        raise DemoLifecycleError("The acceptance instrument must start flat with no active orders.")
    for route, label in (
        ("/protection/handoffs", "protection handoff"),
        ("/protection/incidents", "protection incident"),
    ):
        if any(row.get("inst_id") == inst_id for row in data_rows(api.get(route), label)):
            raise DemoLifecycleError(f"Resolve the existing {label} before Demo acceptance.")

    instruments = data_rows(
        api.get(f"/market/instruments?{urlencode({'inst_id': inst_id})}"), "Instruments",
    )
    if len(instruments) != 1 or instruments[0].get("instId") != inst_id or instruments[0].get("state") != "live":
        raise DemoLifecycleError("Acceptance instrument metadata is unavailable or not live.")
    lot_size = positive_decimal(instruments[0].get("lotSz"), "Instrument lot size")
    minimum = positive_decimal(instruments[0].get("minSz"), "Instrument minimum size")
    size = (minimum / lot_size).to_integral_value(rounding=ROUND_CEILING) * lot_size
    ticker = api.get(f"/market/ticker?{urlencode({'inst_id': inst_id})}").get("data")
    if not isinstance(ticker, dict) or ticker.get("instId") != inst_id:
        raise DemoLifecycleError("Acceptance ticker is unavailable.")
    price = positive_decimal(ticker.get("last"), "Ticker price")
    return status, size, price


def cancel_active_orders(api: ApiClient, inst_id: str, rows: list[dict]) -> None:
    for order in active_orders(rows, inst_id):
        client_id = str(order.get("client_order_id") or "")
        if not client_id or len(client_id) > 32:
            raise DemoLifecycleError("An active order cannot be safely canceled by client identity.")
        result = api.post(f"/execution/orders/{client_id}/cancel")
        if result.get("accepted") is not True and result.get("idempotent") is not True:
            raise DemoLifecycleError("An active Demo order could not be canceled.")


def close_position(api: ApiClient, inst_id: str, position: dict, source: str) -> str:
    try:
        size = abs(Decimal(str(position.get("size") or "0")))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise DemoLifecycleError("Position size is invalid.") from error
    if not size.is_finite() or size <= 0:
        raise DemoLifecycleError("Position size is invalid.")
    payload = signal_payload(inst_id, "close", source=source, position_pct=0.0)
    payload.update(size=number_value(size), dry_run=False)
    result = api.post("/execution/signals", payload)
    order = result.get("order") or {}
    if result.get("accepted") is not True or result.get("dry_run") is not False:
        raise DemoLifecycleError("Demo close signal was not accepted by the execution engine.")
    client_id = str(order.get("client_order_id") or "")
    if not client_id:
        raise DemoLifecycleError("Demo close signal did not return a client order identity.")
    return client_id


def emergency_lock(api: ApiClient) -> bool:
    api.deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    for path, payload in (
        ("/safety/emergency-stop", {"reason": "OKX Demo lifecycle acceptance ended"}),
        ("/worker/control", {"enabled": False}),
    ):
        try:
            api.post(path, payload)
        except Exception:
            pass
    try:
        safety = api.get("/safety/status")
        worker = api.get("/worker/status")
    except Exception:
        return False
    return (
        safety.get("emergency_stopped") is True
        and safety.get("order_submission_allowed") is False
        and worker.get("enabled") is False
        and worker.get("running") is False
    )


def cleanup_after_failure(api: ApiClient, inst_id: str, source: str, deadline: float) -> bool:
    api.deadline = deadline
    try:
        snapshot = synchronized_snapshot(api)
        non_reduce = [row for row in active_orders(snapshot["orders"], inst_id) if not row.get("reduce_only")]
        cancel_active_orders(api, inst_id, non_reduce)
        snapshot, _ = wait_for_snapshot(
            api, deadline,
            lambda current: not any(
                not row.get("reduce_only") for row in active_orders(current["orders"], inst_id)
            ),
            "Demo opening order cancellation",
        )
        positions = positions_for(snapshot["positions"], inst_id)
        reducing = active_close_orders(snapshot["orders"], inst_id)
        wait_deadline = min(deadline, time.monotonic() + EXISTING_CLOSE_WAIT_SECONDS)
        while positions and reducing and time.monotonic() < wait_deadline:
            time.sleep(0.5)
            try:
                snapshot = synchronized_snapshot(api)
            except DemoLifecycleError:
                continue
            positions = positions_for(snapshot["positions"], inst_id)
            reducing = active_close_orders(snapshot["orders"], inst_id)
        if positions and reducing:
            cancel_active_orders(api, inst_id, reducing)
            snapshot, _ = wait_for_snapshot(
                api, deadline,
                lambda current: not active_close_orders(current["orders"], inst_id),
                "existing Demo close reconciliation",
            )
            positions = positions_for(snapshot["positions"], inst_id)
        if len(positions) > 1:
            raise DemoLifecycleError("Automatic Demo cleanup found ambiguous positions.")
        if len(positions) == 1:
            close_id = close_position(api, inst_id, positions[0], source)
            wait_for_snapshot(
                api, deadline,
                lambda current: (
                    not positions_for(current["positions"], inst_id)
                    and any(row.get("client_order_id") == close_id and row.get("status") == "filled" for row in current["orders"])
                ),
                "automatic Demo position cleanup",
            )
        snapshot = synchronized_snapshot(api)
        cancel_active_orders(api, inst_id, snapshot["orders"])
        snapshot, _ = wait_for_snapshot(
            api, deadline,
            lambda current: not positions_for(current["positions"], inst_id)
            and not active_orders(current["orders"], inst_id),
            "automatic Demo order cleanup",
        )
        return not positions_for(snapshot["positions"], inst_id) and not active_orders(snapshot["orders"], inst_id)
    except Exception:
        return False


def run_probe(*, origin: str, inst_id: str, side: str, timeout: float,
              confirmation: str, output: Path | None) -> dict:
    if output is not None:
        output.unlink(missing_ok=True)
    if confirmation != CONFIRMATION:
        raise DemoLifecycleError("Exact Demo lifecycle confirmation is required.")
    validate_environment()
    started_at = utc_now()
    started = time.monotonic()
    deadline = started + timeout
    source = f"demo-lifecycle-{started_at.strftime('%Y%m%d%H%M%S')}"
    api = ApiClient(origin, os.environ["ADMIN_API_TOKEN"].strip())
    api.deadline = deadline
    lifecycle_started = False
    cleanup_completed = False
    locked = False
    try:
        status, size, price = verify_initial_state(api, inst_id)
        limits = status.get("risk_limits") or {}
        max_position_pct = positive_decimal(limits.get("max_position_pct"), "Risk position limit")
        max_stop_pct = positive_decimal(limits.get("max_stop_distance_pct"), "Risk stop limit")
        position_pct = float(min(max_position_pct, Decimal("100")))
        distance_pct = min(Decimal("1"), max_stop_pct / 2)
        if distance_pct <= 0:
            raise DemoLifecycleError("Risk stop limit does not permit a protected acceptance order.")
        ratio = distance_pct / 100
        if side == "long":
            action, stop, target = "open_long", price * (1 - ratio), price * (1 + ratio)
        else:
            action, stop, target = "open_short", price * (1 + ratio), price * (1 - ratio)
        payload = signal_payload(
            inst_id, action, source=source, position_pct=position_pct,
            entry=price, stop=stop, target=target,
        )
        payload["size"] = number_value(size)

        preview = api.post("/execution/signals", {**payload, "dry_run": True})
        if (
            preview.get("accepted") is not True or preview.get("dry_run") is not True
            or (preview.get("preflight") or {}).get("basis") != "exchange"
            or (preview.get("order") or {}).get("status") != "preview"
        ):
            raise DemoLifecycleError("Exchange-backed Demo preview did not pass.")

        stream_baseline = api.get("/system/status")
        lifecycle_started = True
        submitted = api.post("/execution/signals", {**payload, "dry_run": False})
        opening = submitted.get("order") or {}
        open_client_id = str(opening.get("client_order_id") or "")
        if submitted.get("accepted") is not True or submitted.get("dry_run") is not False or not open_client_id:
            raise DemoLifecycleError("Demo opening signal was not accepted.")

        open_stream = wait_for_private_fill(
            api, deadline, open_client_id,
            (stream_baseline.get("account_stream") or {}).get("last_message_at"),
            "Demo open fill",
            require_algo=True,
            algo_before=(stream_baseline.get("algo_stream") or {}).get("last_message_at"),
        )

        def opened(current):
            position = positions_for(current["positions"], inst_id)
            opening_order = next((
                row for row in current["orders"]
                if row.get("client_order_id") == open_client_id and row.get("status") == "filled"
            ), None)
            protection = next((
                row for row in current["orders"]
                if row.get("client_order_id") == open_stream["protection_client_id"]
                and row.get("inst_id") == inst_id and row.get("order_kind") == "algo"
                and row.get("status") in ACTIVE_ORDER_STATUSES
            ), None)
            fill = next((row for row in current["fills"] if row.get("client_order_id") == open_client_id), None)
            if len(position) != 1 or not opening_order or not protection or not fill:
                return None
            if position[0].get("stop_loss") is None or position[0].get("take_profit") is None:
                return None
            return position[0], protection

        snapshot, opened_state = wait_for_snapshot(api, deadline, opened, "Demo open fill and native protection")
        position, protection = opened_state
        close_client_id = close_position(api, inst_id, position, source)
        close_stream = wait_for_private_fill(
            api, deadline, close_client_id,
            open_stream.get("private_last_message_at"),
            "Demo close fill",
        )

        def closed(current):
            close_order = next((
                row for row in current["orders"]
                if row.get("client_order_id") == close_client_id and row.get("status") == "filled"
            ), None)
            close_fill = next((row for row in current["fills"] if row.get("client_order_id") == close_client_id), None)
            return close_order and close_fill and not positions_for(current["positions"], inst_id)

        snapshot, _ = wait_for_snapshot(api, deadline, closed, "Demo close fill and flat position")
        current_protection = next((
            row for row in snapshot["orders"]
            if row.get("client_order_id") == protection.get("client_order_id")
        ), protection)
        if current_protection.get("status") in ACTIVE_ORDER_STATUSES:
            result = api.post(f"/execution/orders/{current_protection['client_order_id']}/cancel")
            if result.get("accepted") is not True and result.get("idempotent") is not True:
                raise DemoLifecycleError("Native Demo protection could not be finalized after close.")

        snapshot, terminal_protection = wait_for_snapshot(
            api, deadline,
            lambda current: next((
                row for row in current["orders"]
                if row.get("client_order_id") == protection.get("client_order_id")
                and row.get("status") in TERMINAL_ORDER_STATUSES
            ), None),
            "native Demo protection terminal state",
        )
        cleanup_completed = (
            not positions_for(snapshot["positions"], inst_id)
            and not active_orders(snapshot["orders"], inst_id)
        )
        if not cleanup_completed:
            raise DemoLifecycleError("Demo lifecycle did not finish flat with no active orders.")
        locked = emergency_lock(api)
        if not locked:
            raise DemoLifecycleError("Emergency stop could not be verified after Demo acceptance.")
        report = {
            "checked_at": utc_now().isoformat(),
            "scope": SCOPE,
            "instrument": inst_id,
            "side": side,
            "size": format(size.normalize(), "f"),
            "demo": True,
            "proxy_configured": bool(os.getenv("OKX_PROXY_URL", "").strip()),
            "private_stream_verified": bool(close_stream.get("private_last_message_at")),
            "algo_stream_verified": bool(open_stream.get("algo_last_message_at")),
            "dry_run_verified": True,
            "exchange_preflight_verified": True,
            "open_fill_verified": True,
            "native_protection_verified": True,
            "close_fill_verified": True,
            "position_flat_verified": True,
            "protection_terminal_verified": terminal_protection.get("status") in TERMINAL_ORDER_STATUSES,
            "cleanup_completed": cleanup_completed,
            "emergency_stopped": locked,
            "worker_disabled": True,
            "live_execution_allowed": False,
            "order_lifecycle_verified": True,
            "trading_performed": True,
            "real_funds_used": False,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        publish(output, report)
        return report
    except BaseException:
        try:
            if lifecycle_started and not cleanup_completed:
                cleanup_completed = cleanup_after_failure(
                    api, inst_id, source, time.monotonic() + CLEANUP_TIMEOUT_SECONDS,
                )
        finally:
            if not locked:
                locked = emergency_lock(api)
        if lifecycle_started and not cleanup_completed:
            raise DemoLifecycleError(
                "Demo lifecycle failed and automatic cleanup could not prove a flat account; inspect the Demo account immediately."
            ) from None
        if not locked:
            raise DemoLifecycleError(
                "Demo lifecycle failed and emergency stop could not be verified; inspect the service immediately."
            ) from None
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:8000")
    parser.add_argument("--inst-id", default="BTC-USDT-SWAP")
    parser.add_argument("--side", choices=("long", "short"), default="long")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--output", default=str(Path.cwd() / "outputs/okx-demo-lifecycle-verification.json"))
    args = parser.parse_args()
    if not 30 <= args.timeout <= 300 or not math.isfinite(args.timeout):
        parser.error("--timeout must be a finite value between 30 and 300 seconds")
    if not args.inst_id or len(args.inst_id) > 40 or not args.inst_id.endswith("-SWAP"):
        parser.error("--inst-id must be an OKX perpetual instrument")
    try:
        result = run_probe(
            origin=args.origin,
            inst_id=args.inst_id.upper(),
            side=args.side,
            timeout=args.timeout,
            confirmation=args.confirm,
            output=report_path(args.output),
        )
    except DemoLifecycleError as error:
        print(f"OKX Demo lifecycle smoke failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as error:
        print(
            f"OKX Demo lifecycle smoke failed ({type(error).__name__}); no success report was published.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
