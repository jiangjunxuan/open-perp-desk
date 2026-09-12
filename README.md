# OpenPerpDesk

OpenPerpDesk is an open-source web control plane for AI-assisted perpetual
contract trading. It is designed to run on a server with Docker and be used
from a browser.

The planned system combines:

- TradingAgents for research and multi-agent market analysis
- OKX official agent skills or API adapters for market data and execution
- A separate risk engine that must approve every order
- PushPlus for WeChat notifications
- A dense, professional trading-terminal web interface

## Safety status

The repository currently contains a safe initial skeleton only:

- The default mode is `demo`.
- No live order execution is enabled.
- No API credentials are stored in the repository.
- The web shell reads non-secret status from the API.
- The execution worker is intentionally not connected yet.

Do not use this project with live funds until the execution, reconciliation,
risk, and failure-mode tests are complete.

## Start locally

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8080`.

The API health endpoint is available through the web proxy at
`/api/v1/health`.

## Planned capabilities

- Real-time market watchlists and charts
- Perpetual contract positions, orders, margin, leverage, and PnL
- AI research reports and structured trade signals
- Backtesting and strategy comparison
- Risk limits, take-profit, stop-loss, and emergency stop
- Demo trading before any live mode
- OKX REST/WebSocket connectivity with optional outbound SOCKS5/HTTP proxy
- PushPlus alerts for signals, fills, risk events, and system failures
- Audit logs, account permissions, and Docker-based deployment

## Repository layout

```text
apps/web/       Browser-facing web shell
services/api/   FastAPI control-plane API
docs/           Architecture, security, and delivery notes
docker-compose.yml
```

## License

MIT. See `LICENSE`.

This software is for research and automation engineering. It is not financial
advice and does not promise profits.

