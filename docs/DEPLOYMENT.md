# OpenPerpDesk 部署说明

本文面向单台 Linux 服务器上的 Docker Compose 部署。默认目标是 OKX Demo；
不要把真实 API 密钥写进 Git、镜像层或前端文件。

## 1. 准备环境

需要：

- Docker Engine 和 Compose v2
- Python 3.11 或更新版本（部署脚本仅使用标准库）
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
chmod +x infra/openperpdesk.sh
```

编辑 `.env`，至少设置一个随机的 `ADMIN_API_TOKEN`。首次运行保持：

本机 `APP_ENV=development` 或 `test` 可以暂时使用 `admin` 进行页面联调；
生产和预发布环境仍强制要求至少 16 位令牌，不能沿用这个开发密码。

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
./infra/openperpdesk.sh preflight
./infra/openperpdesk.sh up
./infra/openperpdesk.sh status
./infra/openperpdesk.sh smoke
python infra/realtime-smoke.py --base-url http://127.0.0.1:8080
```

`preflight` 会在不打印密钥的前提下检查 `.env` 权限、Demo/实盘模式、
管理员令牌、执行凭据、自动 Worker、TradingAgents 路径和代理协议。
启用 TradingView 时，还会检查独立 Webhook 密钥、非空合约白名单、时效和
数值参数；生产密钥至少 32 个字符，非 Dry Run 执行仍需全局执行开关。
校验输入来自 `docker compose config --format json` 的最终解析结果，包含
Compose 默认值、环境变量覆盖和覆盖文件，不再手工解析 `.env`。
Demo 部署要求 `TRADING_MODE=demo`、`OKX_DEMO=true` 且实盘闸门关闭；
Live 模式必须保持 `OKX_DEMO=false`；需要执行时才额外要求
`LIVE_TRADING_ENABLED=true`、`EXECUTION_ENABLED=true` 和人工解锁短语。
执行关闭的 Live 配置可用于只读核对和恢复，但不会获得下单权限。
`up` 和 `restart` 会自动再次执行 preflight，检查失败时不会启动或重启服务。
`smoke` 除了检查 Web、图标、健康和 readiness，还会验证未带令牌的私有接口返回
401，以及使用已配置管理员令牌后能够通过鉴权；账户凭据未配置时允许接口返回
明确的只读空态。

`api` 和 `web` 都带有 Compose healthcheck；API 的优雅停止时间为 30 秒，
Web 为 15 秒，便于升级时让 WebSocket 和正在处理的请求自然结束。
Uvicorn 在 20 秒后终止仍未结束的连接，避免空闲 SSE 阻止容器退出；
未确认订单仍保留持久化占位，重启后只读查单恢复，不自动重发。
`/api/v1/health` 是进程存活检查；`/api/v1/health/readiness` 会在公共行情
断线或状态库不可用时返回 `503`，适合接入反向代理或外部监控；运行指标可从
`/api/v1/health/metrics` 读取，返回内容不包含代理地址、令牌或 API 密钥。

默认 Compose 卷 `openperpdesk-data` 挂载到 API 的 `/data`。数据库通常为
`DATA_DIR/openperpdesk.sqlite3`；`STATE_DB_PATH` 可覆盖为例如
`/data/state/control.sqlite3`。部署脚本要求最终数据库路径处于可写的持久卷中；
不能放到容器临时层、只读挂载或被嵌套 tmpfs 遮盖的位置。不要把数据卷映射到 Web。

升级前先查看状态并备份数据库：

```bash
./infra/openperpdesk.sh status
./infra/openperpdesk.sh backup
```

## 3. 宝塔反向代理

在宝塔中创建站点，例如 `perp.example.com`，反向代理到：

```text
http://127.0.0.1:8080
```

默认 `WEB_BIND_ADDRESS=127.0.0.1`，避免绕过宝塔的 HTTPS 和访问控制直接访问端口。
确需对外暴露端口时可显式修改，但应同时设置服务器防火墙。`WEB_PORT` 可独立调整。

启用 HTTPS 后，确认以下请求都走同一域名：

- `/`：Web 控制台
- `/api/v1/health`：健康检查
- `/api/docs`：API 文档（生产环境可通过 Nginx 访问控制隐藏）

不要把 `ADMIN_API_TOKEN` 放在 Nginx 配置、URL、前端 HTML 或浏览器 localStorage
中。控制台只在当前页面内存中发送 `X-Admin-Token`。

实时推送的三个路径是 `/api/v1/market/events`、`/api/v1/system/events`
和 `/api/v1/account/events`。宝塔外层代理应关闭这些路径的响应缓冲与缓存：

```nginx
location ~ ^/api/v1/(market|system|account)/events$ {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 60s;
}
```

不要新增公开 API 端口或绕过站点访问控制。配置后用实际 HTTPS 域名运行
`infra/realtime-smoke.py --base-url https://perp.example.com`，脚本只读检查
连续事件、心跳时延及私有通道未认证时的拒绝结果，不会发单。

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

代理认证失败不会回退为直连，行情 REST 传输异常只返回异常类型，不返回代理配置。
SOCKS5 握手失败时会主动清理尚未交给 HTTP 连接管理的套接字；相关处理使用
固定版本 `httpcore` 的请求级 trace 扩展。升级 HTTP 依赖时，必须重跑
`test_http_transport`、`test_market_transport_safety` 和 `test_okx_stream_transport`，
确认连接释放、错误脱敏、取消和禁止直连行为不变。

公共报价使用 `OKX_WS_PUBLIC_URL`（默认 `/ws/v5/public`），公共 1 分钟 K 线
使用 `OKX_WS_CANDLES_URL`（默认 `/ws/v5/business`），两者独立连接和记录新鲜度。
`OKX_WS_BUSINESS_URL` 则用于需要登录的止盈止损订单流，留空时按 Demo/实盘
选择地址，不要将它与公共 K 线端点混淆。所有连接共用 `OKX_PROXY_URL`。

部署后可先运行只读代理验收：

```bash
python infra/proxy-smoke.py --timeout 45
```

它会同时验证 OKX REST、公共报价和 `1m`、`15m`、`1H`、`4H` K 线；
只输出脱敏连接证据，不读取私有账户，不执行交易。

WebSocket 非本机端点必须使用 `wss://` 并校验证书；`ws://` 仅允许
回环目标与本机测试代理。

配置 OKX Demo 私有凭据并更新 API 容器后，用下面的命令在运行中的容器内
验证账户、持仓、订单、成交和原生保护订单流：

```bash
./infra/openperpdesk.sh private-smoke --timeout 45
```

该命令通过标准输入运行只读脚本，使用现有 API 容器的 OKX 密钥、端点和代理，
无需在主机安装 API 依赖，也不会把密钥放进命令行。尚未重新创建容器的 `.env`
修改不会自动生效。它不导入 API 主程序或执行客户端，不启动 Worker，不发交易请求。
主机报告保存在 `outputs/okx-private-verification.json`，权限为 `600`，
只包含连接状态和记录数量；失败时不沿用旧成功文件。

`--timeout` 限制 WebSocket 登录与全部 REST 查询的总时长，取值 5 至 300 秒，
另留连接关闭时间。查询结束时两条私有 WebSocket 必须仍处于连接且认证状态。
登录与只读列表通过并不证明订单成交、状态推送或持仓保护闭环通过；
报告明确保留 `order_lifecycle_verified=false`、`trading_performed=false`。

默认拒绝 `OKX_DEMO=false`。对真实账户的只读连接也必须显式加 `--allow-live`，
并在人工审批记录中保留命令输出和时间。非 Docker 开发可在已安全导出 OKX 环境变量、
已安装 API 依赖的 Shell 中运行 `python infra/okx-private-smoke.py --timeout 45`；
此直接入口不会自动读取 `.env`。不要用 `source .env` 执行未经审核的文件。

PushPlus 配置后，服务会对关键运行事件发送通知，包括风控拒绝、订单失败、
Worker 启停/异常、急停/恢复、成交回报、原生止盈止损状态变化和账户同步异常。
成交回报按交易所 `tradeId` 去重；没有配置 `PUSHPLUS_TOKEN` 时不会阻塞行情、
风控或 Demo 执行。

PushPlus 的同步 `code=200` 仅表示接口受理，不代表微信送达。测试接口
`POST /api/v1/notifications/test` 返回 `accepted=true`、`delivery_confirmed=false`，
并在可用时携带消息流水号；审计记录为 `notification_accepted`，不会标为送达成功。
上游消息正文和原始异常不会进入 API 响应，响应体超过 64 KiB 或格式无法识别时
按受理状态未知处理。明确拒绝、未配置和状态未知使用不同的固定错误码。
网络失败或 503 之后不自动重发测试通知，因为对端可能已经受理；应先在微信或
PushPlus 记录核对，避免重复消息。实时行情读取的自动重试不受此限制。
控制台会阻止重复点击，并忽略管理员权限变更前发出的迟到响应。
当前没有将 PushPlus 回调作为已验证的微信送达凭据，真实收信仍需单独验收。

接口语义参考 PushPlus 官方发送 API：
`https://www.pushplus.plus/doc/guide/api.html`。

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
TRADINGAGENTS_LLM_PROVIDER=openai_compatible
TRADINGAGENTS_DEEP_THINK_LLM=<tested-model>
TRADINGAGENTS_QUICK_THINK_LLM=<tested-model>
TRADINGAGENTS_LLM_BACKEND_URL=https://your-openai-compatible-endpoint/v1
OPENAI_COMPATIBLE_API_KEY=<server-side-secret>
TRADINGAGENTS_CACHE_DIR=/data/tradingagents/cache
TRADINGAGENTS_RESULTS_DIR=/data/tradingagents/results
TRADINGAGENTS_MEMORY_LOG_PATH=/data/tradingagents/memory/trading_memory.md
TRADINGAGENTS_TIMEOUT_SECONDS=300
# Bound Yahoo/yfinance calls inside the disposable research process.
TRADINGAGENTS_DATA_TIMEOUT_SECONDS=8
TRADINGAGENTS_MAX_OUTPUT_BYTES=4194304
TRADINGAGENTS_MAX_TOKENS=4096
TRADINGAGENTS_LLM_MAX_RETRIES=1
```

默认构建不安装 TradingAgents 重依赖；可选 Dockerfile 会在构建时克隆
`TRADINGAGENTS_REPOSITORY` 的 `TRADINGAGENTS_REF`。生产环境应固定已测试的
tag 或 commit，不要直接依赖不受控的 `main`。当前默认固定在
`be952b8eccb49720509af544c6675233bc1f10d0`，核心依赖受
`services/api/tradingagents-constraints.txt` 约束；这不是完整传递依赖锁，
仍需在目标服务器构建验收。

后台通过独立 Python 子进程运行研究，不再在 API 线程中导入上游框架。
单机非 Docker 开发可配置 `TRADINGAGENTS_PYTHON` 指向独立虚拟环境中的
Python；只挂载源码不能替代安装依赖。默认仍使用 API 所在解释器。

`GET /api/v1/analysis/status` 区分基础配置与运行时状态；目录存在不代表依赖
或模型已验证。管理员调用 `POST /api/v1/analysis/ai/check` 可编译研究图并检查
模型客户端配置，不会发模型请求，因此返回的 `provider_connection_verified`
始终为 `false`。实际研究仍通过 `POST /api/v1/analysis/ai` 发起。

同一持久化目录一次只运行一个研究或自检，重复请求返回 409。研究默认限时
300 秒，允许配置 1 至 1800 秒；超时返回 504 并终止进程组。任务取消、服务
正常关闭会清理研究进程；子进程还监测父进程存活和自身期限，防止 API 异常
退出后继续耗用模型。Compose 的 `init: true` 用于回收子孙进程。

研究输入上限 256 KiB，输出默认上限 4 MiB，最多 16 MiB；每次模型输出默认
4096 token，SDK 最多重试 1 次。另有辩论轮数和图递归上限。所有这些限制
都只限制运行成本，不能证明模型结论正确。

内置 Nginx 为 AI 研究路径单独设置 1860 秒读取超时。宝塔或其他外层反向
代理也需要为此路径设置足够的超时，否则代理可能先断开，但服务端研究仍会
在上述硬期限内结束。该接口当前是同步请求，不是可恢复的后台作业队列。

研究进程只接收白名单中的模型/公共数据提供方环境变量和基础 TLS 配置，
不继承 OKX 凭据、管理员令牌、PushPlus token、实盘解锁短语或代理认证。
临时 HOME 和工作目录在持久化数据目录之下，每次运行后清除；缓存、报告
和研究记忆保持在配置的存储路径。原始第三方 stderr 不回传客户端，避免
异常对象泄露密钥。这里只是环境及生命周期隔离，不是操作系统权限沙箱；
必须信任并审查固定版本的 TradingAgents 及其依赖。

如果使用 OpenAI-compatible 网关，`TRADINGAGENTS_LLM_BACKEND_URL` 和对应的
API key 只放在服务器 `.env`，不要提交到仓库，也不要通过浏览器配置。先单独
验证 AI 分析接口，确认返回结果可序列化后，再考虑启用自动策略；TradingAgents
仍然只属于研究层，不能绕过结构化信号、风控、幂等和安全闸门。

TradingAgents 输出以 `signal={}`、`execution_authorized=false` 保存。
OKX 快照包含所选周期、时间、资金费率、持仓量及缺失项；上游现货/日线数据
仅是补充，不能当成 OKX 永续委托价。研究默认禁用上游断点恢复，避免把新的
市场快照混入旧图状态；研究不能绕过结构化信号、风控、幂等和安全闸门。

本地框架验收（不使用真实模型 key，也不下单）：

```bash
python3 -m venv work/ai-venv
work/ai-venv/bin/python -m pip install \
  -c services/api/tradingagents-constraints.txt ./work/upstream-tradingagents
.venv/bin/python infra/ai-smoke.py
```

该命令要求已检出固定版本源码且本地预览 API 正在运行。它使用真实框架、
本地模型协议服务与 OKX 公共快照，校验多代理和结构化输出，结果写入
`outputs/ai-framework-verification.json`；不能替代真实模型服务、外部新闻工具
或服务器容器验收。

## 6. 备份、恢复与回滚

备份通过 SQLite Online Backup API 生成一致性单文件快照，不直接复制正在写入的主库。
备份命令使用运行中容器的真实 `STATE_DB_PATH` / `DATA_DIR`，能包含尚未 checkpoint
到主文件的已提交 WAL 数据。主机收到后再次做完整性及应用表结构校验，成功后原子发布。
每份备份附带 SHA-256 JSON 清单；备份目录权限为 `700`，快照及清单为 `600`。

```bash
./infra/openperpdesk.sh backup
```

恢复前在部署配置中明确保持以下值，脚本会检查最终 Compose 环境，不满足则拒绝停机：

```dotenv
EXECUTION_ENABLED=false
LIVE_TRADING_ENABLED=false
AUTO_TRADING_ENABLED=false
AUTO_TRADING_DRY_RUN=true
```

恢复命令会自行停止 API，不需要手工先停止。先确认将使用的 API 镜像已包含当前维护模块；
首次升级到这套工具时可先运行 `docker compose build api`，该命令不会替换运行中的容器。

```bash
./infra/openperpdesk.sh restore ./backups/openperpdesk-<timestamp>.sqlite3
```

恢复流程：

1. 校验源文件格式、SQLite 完整性、应用结构及已有 SHA-256 清单；先在主机上生成受保护的
   一致性中间快照，避免传输过程中源文件变化或漏掉源文件旁的 WAL 提交。
2. 核对镜像和正在运行容器的数据库路径，拒绝配置漂移；停止 API 并确认没有运行实例。
3. 在数据卷内暂存并校验传输数据，保存旧主库和 WAL/SHM/journal 文件到
   `数据库目录/recovery/before-<时间>-<ID>/raw/`。
4. 旧库可读取时，额外生成该归档下的 `rollback.sqlite3`。旧库已损坏时仍保留原始文件，
   但不会把它宣称为可恢复的有效快照。
5. 在候选恢复库中触发持久急停、停用策略并记录审计事件；不改变备份源文件。
6. 写入恢复中标记后，原子替换主库。若进程中断，状态库拒绝初始化和读写，
   不会把半恢复状态当成正常账户继续运行。
7. 强制重建 API 容器，验证执行关闭、Worker 关闭、Dry Run 开启、实盘闸门关闭及急停状态。
   随后重建 Web，避免 Nginx 保留旧 API 地址，并检查页面、图标和 API 存活。

恢复验收失败时脚本尝试保持 API 停止，并保留回滚归档，不自动启用任何交易。
恢复中断后，可在 API 已停止、相同安全配置下重新执行完整恢复命令。不要手工删除
`.数据库文件名.restore-in-progress` 标记来绕过恢复检查。

需要回滚到恢复前的有效快照时，先通过 `docker compose cp` 将归档中的
`rollback.sqlite3` 导出到受保护的主机备份目录，再使用同一个 `restore` 命令。
没有有效 `rollback.sqlite3` 的损坏旧库只能作为排障证据，不能保证回滚成功。

脚本还提供 `restart`、`logs`、`down` 和 `smoke` 命令。它会拒绝权限不是
`600` 的 `.env`，备份目录默认为项目下的 `backups/`，不会打印环境变量内容。
同一项目下的部署、备份和恢复命令使用操作锁互斥。操作期间不要另行运行 Compose
或修改环境配置。`down` 不会使用 `-v` 删除数据卷。备份与数据库已加入 Git 和构建排除。

升级采用可回滚方式：

1. 备份 `.env` 和 SQLite。
2. `docker compose pull` 或更新代码。
3. `./infra/openperpdesk.sh up`，该命令同时刷新 Nginx 的 API 地址。
4. 检查 `/api/v1/health`、`/api/v1/system/status`、Web 页面和日志。
5. 若失败，恢复上一个 Git 版本和数据库备份，再执行 `up -d --build`。

恢复后的数据库可能早于交易所最新订单，必须核对账户、活动委托、成交和未知提交状态。
急停不会撤销交易所已有订单。确认核对完整、策略参数正确后，才可由管理员恢复并另行启用
Demo Worker。任何实盘试运行必须另行
人工审批，不属于常规升级流程。

## 验证边界

本地保护兜底使用 OKX `GET /api/v5/public/mark-price` 的标记价格，与原生止盈止损
的 `mark` 触发类型一致，不以最新成交价代替。返回必须仅包含目标 SWAP 合约，
价格为有限正数，时间戳不早于本机 30 秒且不超前 5 秒。标记价格缺失、过期或
请求失败时，该合约本轮不会进行本地保护判断或新策略决策，并产生审计和通知事件；
下一轮重新读取。已有交易所原生保护单不会被此流程取消。
这是 Worker 周期检查的兜底，不替代交易所持续运行的原生保护，也不保证成交价格。
本地协议测试覆盖最新价与标记价不同步、价格端点失效、过期价格和恢复后的只减仓请求。
接口字段参考 OKX 官方文档 `https://www.okx.com/docs-v5/en/#public-data-rest-api-get-mark-price`。

本地账本观测到持仓归零、关闭后重开或净持仓方向反转时，会原子递增持仓周期编号，
使新周期的保护平仓不复用旧周期的幂等结果；账户身份或已验证保护关联改变时也会
更新编号。同一已验证关联下的重复快照和重启不会单独更换编号。旧数据库只增加
关联字段，缺少依据的旧保护价须等待新的有效账户快照核实，不凭空继承；
原有订单幂等记录完整保留。仍有同方向普通平仓单
处于提交、成交中、撤销中或结果未知状态时，Worker 不追加平仓请求，须先完成对账。
共用交易前校验也检查交易所活动普通委托和本地未确认平仓，结合原子发单代际校验
拦截不同请求编号的并发重复平仓；同一编号的重放仍返回原请求状态。
这里的周期基于已观测到账本的持仓变化，不能声称识别了断线期间未观测到的所有交易。
保护关联和发单前的持仓成交身份二次核验见 `ORDER_RECOVERY.md`。
已完全成交的单笔开仓还须关联当前原生保护参数；外部撤销、触发或改成不支持的
触发/执行方式时，本地兜底暂停，不继续按原开仓保护价平仓。
原生保护缺失的精确查询失败会保留未解决状态并阻止 Worker 当轮交易，
不证明交易所已经撤单。多笔合并持仓的分单本地保护仍未开放。

本地测试覆盖真实 SQLite WAL 快照、并发写入时的一致性、坏库拒绝、回滚归档、
自定义路径、标准输入传输校验、恢复子进程强制退出和持久启动拦截。
本地 Compose 编排测试使用命令契约替身，不代表容器已经实际运行。
CI 基线 `b58edbc` 已在 2026-09-13 实际构建镜像、验证图标、在自定义数据路径
执行备份恢复及重启持久性；可选 TradingAgents 镜像也通过离线非 root 自检。
实时 SSE 版本 `0c4280e` 已通过 CI `34750347409`：容器 Nginx 前后两轮流式检查通过，
恢复重启后连续行情事件间隔为 0.250 秒、系统心跳为 5.005 秒，未认证私有请求返回 401，
执行关闭和急停开启均已校验。此记录不替代后续版本自己的 CI。
当前开发机没有 Docker，目标服务器 HTTPS、宝塔和实际外部凭据仍需另行验收；
离线研究自检不能证明真实模型连接可用。

## 7. 最小上线检查

正式进入真实资金环境前，必须完成并留存
[`docs/LIVE_APPROVAL_CHECKLIST.md`](LIVE_APPROVAL_CHECKLIST.md)；
本节只是部署存活检查，不是实盘批准。

- [ ] 域名 HTTPS 可访问
- [ ] `./infra/openperpdesk.sh preflight` 通过
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
