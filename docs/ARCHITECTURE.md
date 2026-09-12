# Architecture

## Runtime boundary

The browser is a control and observability surface. It is not the trading
engine. The trading worker must continue to operate when the browser is
closed, and it must not trust commands from an unauthenticated client.

```text
Browser
  |
  v
Reverse proxy / HTTPS
  |
  +--> Web frontend
  +--> Control-plane API
              |
              +--> PostgreSQL
              +--> Redis
              +--> Analysis service
              +--> Risk engine
              +--> Order executor ---> OKX REST/WebSocket
                                            ^
                                            |
                                  optional SOCKS5/HTTP egress proxy
```

## Order lifecycle

1. Market adapters normalize OKX market and account events.
2. Strategy and TradingAgents produce a structured signal.
3. The risk engine validates freshness, exposure, leverage, loss limits, and
   duplicate-order guards.
4. The executor submits an order only after the risk engine approves it.
5. Private WebSocket events and REST reconciliation confirm the actual state.
6. The audit log records the decision, order, fills, and risk checks.
7. PushPlus sends a concise notification for configured events.

The AI layer must never be the only source of truth for balances, positions,
orders, fills, or risk limits.

## Initial implementation choices

- FastAPI for the control-plane API
- A browser web shell served by Nginx
- Docker Compose for local and server deployment
- PostgreSQL for durable state
- Redis for transient state and worker coordination
- Demo trading as the default environment

