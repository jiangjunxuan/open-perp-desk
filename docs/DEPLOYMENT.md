# OpenPerpDesk 部署说明

本文面向单台 Linux 服务器上的 Docker Compose 部署。默认目标是 OKX Demo；
不要把真实 API 密钥写进 Git、镜像层或前端文件。

## 1. 准备环境

需要：

- Docker Engine 和 Compose v2
- 一个可解析到服务器的域名
- 宝塔可选，用于站点、HTTPS 和反向代理
- 至少一个独立的服务器目录保存 Compose 文件和 `.env`

```bash
mkdir -p /opt/openperpdesk
cd /opt/openperpdesk
git clone <repository> .
cp .env.example .env
mkdir -p backups
chmod 600 .env
```

编辑 `.env`，至少设置一个随机的 `ADMIN_API_TOKEN`。首次运行保持：

```dotenv
APP_ENV=production
TRADING_MODE=demo
OKX_DEMO=true
EXECUTION_ENABLED=false
AUTO_TRADING_ENABLED=false
AUTO_TRADING_DRY_RUN=true
# 留空时按 OKX_DEMO 自动选择业务 WebSocket 地址
OKX_WS_BUSINESS_URL=
```

风控默认还会限制全部活动合约的总名义敞口：

```dotenv
RISK_MAX_TOTAL_NOTIONAL_PCT=30
```

## 2. 启动与验证

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8080/api/v1/health
curl -fsS http://127.0.0.1:8080/api/v1/system/status
curl -fsS http://127.0.0.1:8080/api/v1/health/readiness
```

`api` 和 `web` 都带有 Compose healthcheck；API 的优雅停止时间为 30 秒，
Web 为 15 秒，便于升级时让 WebSocket 和正在处理的请求自然结束。
`/api/v1/health` 是进程存活检查；`/api/v1/health/readiness` 会在公共行情
断线或状态库不可用时返回 `503`，适合接入反向代理或外部监控；运行指标可从
`/api/v1/health/metrics` 读取，返回内容不包含代理地址、令牌或 API 密钥。

`api` 服务的 `/data` 是唯一需要持久化的应用数据目录。Compose 卷
`openperpdesk-data` 中保存 SQLite 文件；不要把它映射到 Web 容器。

升级前先查看状态并备份数据库：

```bash
docker compose exec -T api python -c \
  'import os, sqlite3; path=os.path.join(os.getenv("DATA_DIR", "/data"), "openperpdesk.sqlite3"); connection=sqlite3.connect(path); print(connection.execute("PRAGMA integrity_check").fetchone()[0]); connection.execute("PRAGMA wal_checkpoint(TRUNCATE)"); connection.close(); print(path)'
docker compose cp api:/data/openperpdesk.sqlite3 \
  "./backups/openperpdesk-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
```

## 3. 宝塔反向代理

在宝塔中创建站点，例如 `perp.example.com`，反向代理到：

```text
http://127.0.0.1:8080
```

启用 HTTPS 后，确认以下请求都走同一域名：

- `/`：Web 控制台
- `/api/v1/health`：健康检查
- `/api/docs`：API 文档（生产环境可通过 Nginx 访问控制隐藏）

不要把 `ADMIN_API_TOKEN` 放在 Nginx 配置、URL、前端 HTML 或浏览器 localStorage
中。控制台只在当前页面内存中发送 `X-Admin-Token`。

## 4. OKX 出站代理

交易引擎和行情流通过服务端环境变量出站，不经过浏览器代理：

```dotenv
OKX_PROXY_URL=socks5h://user:password@proxy.example.com:1080
```

HTTP 代理可以使用：

```dotenv
OKX_PROXY_URL=http://user:password@proxy.example.com:8080
```

PushPlus 可独立设置：

```dotenv
PUSHPLUS_PROXY_URL=http://user:password@proxy.example.com:8080
```

代理凭据只写入服务器 `.env`，并确保 `chmod 600 .env`。系统状态接口只返回
是否配置代理，不返回代理地址。

## 5. TradingAgents

TradingAgents 是可选分析依赖，不应阻塞结构化策略和风控链。生产部署有两种方式：

1. 使用本仓库提供的可选 Compose 覆盖文件，从指定仓库和版本构建 API 镜像：

   ```bash
   TRADINGAGENTS_REF=<tested-ref> \
   docker compose -f docker-compose.yml \
     -f docker-compose.tradingagents.yml build api
   ```

2. 将已安装并经过兼容性测试的 TradingAgents 源码挂载到 API 容器的
   `TRADINGAGENTS_PATH`。

启用前先保持 `EXECUTION_ENABLED=false`，只测试：

```dotenv
TRADINGAGENTS_ENABLED=true
TRADINGAGENTS_PATH=/opt/tradingagents
TRADINGAGENTS_OUTPUT_LANGUAGE=Chinese
TRADINGAGENTS_LLM_PROVIDER=openai
TRADINGAGENTS_DEEP_THINK_LLM=<tested-model>
TRADINGAGENTS_QUICK_THINK_LLM=<tested-model>
TRADINGAGENTS_LLM_BACKEND_URL=https://your-openai-compatible-endpoint/v1
OPENAI_COMPATIBLE_API_KEY=<server-side-secret>
TRADINGAGENTS_CACHE_DIR=/data/tradingagents/cache
TRADINGAGENTS_RESULTS_DIR=/data/tradingagents/results
TRADINGAGENTS_MEMORY_LOG_PATH=/data/tradingagents/memory/trading_memory.md
```

默认构建不安装 TradingAgents 重依赖；可选 Dockerfile 会在构建时克隆
`TRADINGAGENTS_REPOSITORY` 的 `TRADINGAGENTS_REF`。生产环境应固定已测试的
tag 或 commit，不要直接依赖不受控的 `main`。

如果使用 OpenAI-compatible 网关，`TRADINGAGENTS_LLM_BACKEND_URL` 和对应的
API key 只放在服务器 `.env`，不要提交到仓库，也不要通过浏览器配置。先单独
验证 AI 分析接口，确认返回结果可序列化后，再考虑启用自动策略；TradingAgents
仍然只属于研究层，不能绕过结构化信号、风控、幂等和安全闸门。

TradingAgents 输出只能作为研究层结果；它不能绕过结构化信号、风控、幂等和安全闸门。

## 6. 备份、恢复与回滚

恢复前停止 API，避免 SQLite 写入竞争：

```bash
docker compose stop api
docker compose cp ./backups/openperpdesk-<timestamp>.sqlite3 \
  api:/data/openperpdesk.sqlite3
docker compose start api
curl -fsS http://127.0.0.1:8080/api/v1/health
```

升级采用可回滚方式：

1. 备份 `.env` 和 SQLite。
2. `docker compose pull` 或更新代码。
3. `docker compose up -d --build`。
4. 检查 `/api/v1/health`、`/api/v1/system/status`、Web 页面和日志。
5. 若失败，恢复上一个 Git 版本和数据库备份，再执行 `up -d --build`。

恢复期间保持 `EXECUTION_ENABLED=false`、`AUTO_TRADING_ENABLED=false`，
确认账户、订单和急停状态后再考虑开启 Demo Worker。任何实盘试运行必须另行
人工审批，不属于常规升级流程。

## 7. 最小上线检查

- [ ] 域名 HTTPS 可访问
- [ ] `/api/v1/health` 返回 `status=ok`
- [ ] `/api/v1/health/readiness` 返回 `ready=true`
- [ ] `/api/v1/health/metrics` 未泄露代理地址和密钥
- [ ] `TRADING_MODE=demo`、`OKX_DEMO=true`
- [ ] `EXECUTION_ENABLED=false`
- [ ] `AUTO_TRADING_ENABLED=false`
- [ ] `.env` 权限为 `600`
- [ ] SQLite 备份已生成并可读取
- [ ] 代理只配置在服务端，状态页不泄露地址
- [ ] PushPlus 测试通知成功或明确记录为未配置
- [ ] 未使用真实资金完成 Demo 账户、订单、成交和急停验证
- [ ] 原生止盈止损 `orders-algo` 业务流已连接并验证状态对账
