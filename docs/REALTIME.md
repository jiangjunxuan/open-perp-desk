# Real-Time Console

The console uses persistent server-sent event (SSE) connections. It does not
require a page reload or a manual account-sync click to display new exchange
events. This is a latest-state trading console, not a lossless tick recorder.

## Data Paths

- OKX public WebSocket -> ticker, funding-rate and open-interest caches ->
  `/api/v1/market/events` -> price, bid/ask, watchlist and market statistics.
- OKX business WebSocket -> native 1m, 15m, 1H and 4H candles -> the same SSE
  connection for the selected chart interval. Candle OHLCV is supplied by OKX,
  not synthesized from ticker prices.
- OKX private/account and business/algo WebSockets -> immediate reconciler
  wake-up -> durable order, fill and position ledger -> `/api/v1/account/events`.
- Committed application changes -> private SSE for orders, fills, positions,
  strategies, analyses, audit events, bill-import progress and bill snapshots.
- Worker, safety and research-runtime state -> `/api/v1/system/events`.
- Actions show an immediate busy state, then the API outcome. Order acceptance,
  exchange-confirmed fills and notification-provider acceptance are distinct.

The server coalesces display changes on a 250 ms delivery interval. This bounds
work for a slow browser and does not change execution-engine timing or discard
durable fills. Exchange publication cadence, network latency and processing
still apply. A real-time display does not imply zero latency or immediate fills.
Strategy evaluation continues on the configured strategy/worker schedule.

## Recovery And Privacy

The feeds send a heartbeat at least every five seconds. Browsers detect missing
heartbeats, reconnect with backoff and receive a complete current snapshot.
Background tabs close their feeds and reopen on return. Pausing the chart stops
its live display without stopping execution or account events.
The browser's execution controls stay locked during reconnection until a new
control-state snapshot and fresh market event arrive; a heartbeat alone cannot unlock them. Late
REST status responses cannot replace a newer pushed safety state.
The quote display uses SSE only. It does not substitute periodically fetched
REST tickers when the stream is disconnected. Normal UI status is "已连接";
disconnects are explicit and browser order submission remains locked.

Private feeds require the administrator token in the `X-Admin-Token` header.
Tokens are never put in URLs or persistent browser storage. Revocation stops
private delivery and clears the displayed private state. Public feeds contain
only the already-public market and system-status information.

REST candle history loads on entry, interval changes, manual refresh and
reconnection to repair gaps; there is no 15-second market refresh timer.
Periodic account reconciliation remains separate from display updates.
Position reconciliation distinguishes a newly received WebSocket message from
a replayed cache entry. An older known exchange update time cannot replace a
newer position, and a REST request cannot overwrite or close a position changed
by a push while that request was in flight unless its exchange time proves it
is newer. Protection refreshes use the current stored size, not cached size.
Funding, interest and historical accounting that have not reached the ledger
are not presented as verified live values. Historical FX-valued NAV remains
unfinished; quarterly archive tasks now publish their progress through the
private feed but still need real OKX file acceptance.

## Deployment

The standard deployment runs one API process. Its commit revision is local to
that process; multiple API workers require a shared event bus before scaling.

The bundled Nginx SSE locations disable proxy buffering and caching. An outer
BaoTa/Nginx reverse proxy must also preserve streaming: disable buffering,
avoid response transformations and set a read timeout longer than the heartbeat.
Application shutdown has a bounded grace period so idle SSE clients cannot
prevent a container replacement forever.

## Verification Boundaries

`services/api/tests/test_realtime.py` uses real loopback HTTP/WebSocket servers
and an isolated database to verify private authentication, multi-client safety
updates and order/fill persistence without manual reconciliation.
`services/api/tests/test_realtime_positions.py` covers eight WS/REST position
ordering cases, including delayed responses, closures, replayed caches and
protection updates.
`infra/realtime-ui-checks.mjs` checks the running public feed, pause/resume,
stable focus, stale/incorrect-interval rejection and private UI fixtures.
`infra/chart-annotation-ui-checks.mjs` checks drawing, editing, deletion/undo,
time/price anchors, persistence, corrupt-storage recovery and mobile layouts.

Private fixtures are not real OKX Demo acceptance. Actual private credentials,
model-provider connectivity, PushPlus WeChat receipt, prolonged operation and
target-server HTTPS/proxy acceptance still require separate verification.

On 2026-09-13, commit `0c4280e` passed CI run `34750347409`: 316 API tests,
11 Node tests, the Compose backup/restore/restart drill with SSE through Nginx
before and after recovery, and the optional offline TradingAgents image check.
The local browser run also passed 63 route checks and desktop/mobile live-feed
checks. The image self-check did not connect to a real model provider.
