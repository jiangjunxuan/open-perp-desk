# OpenPerpDesk

OpenPerpDesk 是一个开源的 AI 辅助永续合约交易后台。它运行在服务器上，
通过浏览器访问，并使用 Docker 部署。

主要组成：

- TradingAgents：研究分析和多智能体市场研判
- OKX REST/WebSocket 适配层：行情和交易能力
- 独立风控引擎：每一笔订单都必须经过审核
- PushPlus：微信通知
- 高信息密度、专业交易终端风格的 Web 界面

## 当前安全状态

当前仓库已经包含可运行的 Demo 交易闭环，并具备一条默认关闭的实盘执行路径；
它仍不是可直接投入真实资金的成品：

- 默认模式为 `demo` 模拟盘
- 实盘路径受独立安全闸门保护，默认禁止下单；未完成真实环境验收前不得开启
- 仓库不保存任何 API 密钥
- Web 页面可读取公开行情和不敏感的系统状态
- 私有账户总览需要 `X-Admin-Token`；账户适配器已实现但默认未配置密钥
- 私有账户 WebSocket 已实现基础登录和账户、持仓、订单事件缓存
- 订单 WebSocket 中的有效成交会写入幂等成交账本；REST 对账可校正成交值，
  旧 WS 缓存不会覆盖校正结果，同一成交只通知一次
- 原生止盈止损的 `orders-algo` 业务 WebSocket 已接入，算法订单会进入本地订单账本
- 模拟盘订单客户端、签名、幂等执行和执行闸门已实现；默认仍不会下单
- 预览与执行使用独立编号；实际发单前原子写入订单占位，重复请求不会重复发送
- 超时或进程中断后的发单结果保持待确认，禁止自动重发；需通过交易所对账确认
- 对账支持按订单编号精确查询，验证账户、合约及原始委托身份；旧回报不会重新打开终态订单
- 撤单受理先进入“撤单中”，普通订单和原生算法保护单分别走正确的撤单接口
- 结构化策略、风控预检、Demo 订单预览、止盈止损参数和本地保护兜底已实现
- 自动策略 Worker、历史回测、急停/恢复和审计落盘已实现，自动交易默认关闭
- Worker 周期不会并发运行；已配置账户的权益或快照读取失败时不回退到模拟余额
- Worker 只有显式 `AUTO_TRADING_DRY_RUN=false` 才退出预览；每个执行周期独立限制为 Demo，
  不因实盘客户端已人工解锁而允许这个自动 Worker 实盘下单
- 发单预检使用交易所权益、完整当日账单、真实合约名义金额和待成交敞口；
  跨请求通过原子额度代次防止重复占用同一份预算
- 开仓前设置并确认杠杆，持仓模式与保证金模式由交易所账户和持仓快照核验
- 当日账单包括资金费、交易手续费及已识别扣款；划转不作为交易收益，
  币种无法估值、账单类型未知或分页不完整时拒绝新单
- 私有 REST 对账包含持仓、pending/history 订单和 fills-history，并会关闭交易所快照中已消失的本地仓位
- PushPlus 和 TradingAgents 都是可选集成，未配置时不会影响结构化策略
- 季度历史账单支持服务端申请、等待文件生成、受限下载、CSV 校验、重启续跑和实时进度
- PushPlus 事件通知覆盖风控拒绝、订单提交失败、Worker 启停与异常、急停/恢复、
  成交回报、原生止盈止损状态变化和账户同步异常；重复成交不会重复通知
- 附带保护创建失败会持久化为保护事故，暂停同合约新的开仓，并通过私有 SSE
  推送到后台；管理员可在持仓区填写复核依据后解除，服务端会校验“仓位已归零”口径
- 实盘有独立安全闸门，必须满足多项配置并由管理员在进程内手动解锁，服务重启后自动回锁

在交易执行、持仓校验、风险控制和异常场景测试完成前，不要使用真实资金。
2026-09-19，目标服务器的 Docker/宝塔 HTTPS、公开实时行情、服务端 SOCKS5
代理和 TradingAgents 快速/完整真实模型研究完成只读验收。
2026-09-20 又完成生产同镜像重启、API 进程崩溃自动恢复、当前停写快照实际恢复，
以及生产 API/Web 旧镜像切换与当前镜像返回。
仍需完成真实 OKX Demo 私有账户与订单闭环、PushPlus 微信送达、TradingView Alert、
季度文件和宿主机断电演练。详见 `docs/ROADMAP.md`、
[`2026-09-19 验收记录`](docs/DEPLOYMENT_ACCEPTANCE_2026-09-19.md) 和
[`2026-09-20 恢复记录`](docs/DEPLOYMENT_ACCEPTANCE_2026-09-20.md)。

## 本地启动

```bash
cp .env.example .env
docker compose up --build
```

浏览器打开 `http://localhost:8080`。

API 健康检查地址为 `/api/v1/health`。
生产监控还可以使用 `/api/v1/health/readiness` 判断行情和状态库是否满足
交易前置条件，使用 `/api/v1/health/metrics` 获取不含密钥的运行指标。

只读市场接口包括 `/api/v1/market/ticker`、`/api/v1/market/candles`、
`/api/v1/market/overview` 和 `/api/v1/market/stream`。私有账户总览为
`/api/v1/account/overview`，需要 `X-Admin-Token`。
风险预检接口为 `/api/v1/risk/evaluate`，同样需要 `X-Admin-Token`，且不会提交订单。
统一信号执行入口为 `/api/v1/execution/signals`，支持风控预览和 Demo 执行。
账户同步入口为 `/api/v1/account/sync`；自动 Worker 可通过
`/api/v1/worker/control` 在管理员令牌保护下运行时启停，启用非 Dry Run
前仍必须满足 Demo 执行条件。
旧的直接订单入口已关闭，避免绕过风控链。成交记录和基础 PnL 汇总分别可
通过 `/api/v1/fills`、`/api/v1/performance/pnl` 和
`/api/v1/performance/report` 查看，均需要管理员令牌。
原币种账户账单与当日汇总通过 `/api/v1/account/bills` 查看。
近期历史补录通过 `POST /api/v1/account/bills/imports` 提交包含首尾日期的 UTC
范围，202 只表示受理；历史明细、原币种汇总与覆盖缺口通过
`GET /api/v1/account/bills/history` 查询。绩效页提供对应的查询和进度界面，
支持历史 USD 指数估值与报价缺口核对；尚不计算缺少权益基准的完整账户净值收益。
账单范围、风险损益定义和当前会计限制见 [`docs/ACCOUNTING.md`](docs/ACCOUNTING.md)。

## 当前功能范围

- 实时行情、原生多周期 K 线、合约选择，以及账户/订单/成交/风控状态 SSE 推送（见 `docs/REALTIME.md`）
- 永续合约账户、持仓、订单和运行日志同步
- 结构化策略分析、信号有效期和风控评估
- 历史回测、Demo 预览和信号执行
- 风控限额、原生止盈止损、保护性止盈止损和紧急停止
- 合并持仓分单明细、剩余张数及原生保护数量核对；支持原生撤销确认后的分单接管，
  以及部分成交开仓余单撤销、追加成交和延迟生成保护的接管；原生保护部分触发后，
  先核对子订单终态，再仅平掉该分单余量；手动减仓后的未触发保护会按分单余量自动改量，
  并核验改量结果。Worker 默认关闭，仍有场景待完善，见
  [`docs/POSITION_LOTS.md`](docs/POSITION_LOTS.md)
- 自动策略 Worker，默认关闭且默认只做 Dry Run
- OKX REST/WebSocket 连接，以及可选的 SOCKS5/HTTP 出站代理
- OKX 私有账户流与业务算法订单流，分别用于账户状态和原生止盈止损状态
- PushPlus 通知客户端和测试接口
- TradingAgents 可选适配器
- 成交历史同步、已实现 PnL、手续费和净 PnL 汇总
- 单币种成交绩效：收益率、峰值回撤、日汇总、策略汇总和权益曲线；
  缺少统一估值的混合币种不会被相加，成交绩效不含资金费
- 原币种账户账单：交易损益、手续费、资金费及其他扣款，按账户范围幂等落盘
- 近期历史账单补录：完整 UTC 日原子发布、可重试、覆盖缺口和进度查看，
  原币种永续损益与账户划转分别汇总
- SQLite 本地状态落盘和 Docker Compose 部署
- Docker、宝塔反向代理、HTTPS、服务端 SOCKS5/HTTP 代理、SQLite 备份恢复说明见
  [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- TradingAgents 需要额外依赖时，可使用 `docker-compose.tradingagents.yml` 构建可选 API 镜像

界面设计基线见 [`docs/DESIGN_SYSTEM.md`](docs/DESIGN_SYSTEM.md)，当前 Web 控制台为中文，
默认深色并支持浅色切换，设计方向是高密度、低干扰的交易后台。

## 本地验证

```bash
PYTHONPATH=services/api .venv/bin/python -m unittest discover -s services/api/tests
node --check apps/web/app.js
bash -n infra/openperpdesk.sh
```

API 在本地运行后，可以使用 Node.js 22+ 和 Chrome 检查真实浏览器布局：

```bash
OPENPERPDESK_ORIGIN=http://127.0.0.1:8099 node infra/ui-smoke.mjs
```

脚本使用独立临时 Chrome 配置，不读取日常浏览器数据。Linux 默认调用
`google-chrome`，其他位置可通过 `CHROME_BIN` 指定。Linux 验收环境还需中文字体，
例如 Ubuntu 的 `fonts-noto-cjk`；测试会检查中文字符实际绘制，缺字方框不能通过验收。
它验证 1440/1024/844 横屏/768/390/375/320px
视口无页面级横向溢出、九个导航视图、深链接与后退、解锁弹窗焦点、
K 线画布非空、关注合约切换、暂停刷新、账单原币种金额及缺失估值不会显示为零，并将截图保存到
`outputs/`。该烟测需要公共行情可用，只操作公开行情，不进行账户登录或下单。
管理、研究及历史账单的内容丰富状态使用显式浏览器样本，不连接私有账户；
包含日期查询、游标分页、补录进度、重复点击、失败保留、权限失效和迟到响应检查。

无需真实 OKX 连接的隔离浏览器回归与 CI 使用：

```bash
.venv/bin/python infra/ui-acceptance.py
```

此入口启动临时 API/SQLite 和回环协议测试服务，执行、自动交易和实盘保持关闭，
并断言没有向测试交易所发送任何变更请求。截图、报告与日志保存在 `work/ci-ui-artifacts/`，
不会覆盖 `outputs/` 中真实公共行情的验收截图。它不代替真实 OKX 模拟盘联调。
图表标记的范围与使用方式见 [`docs/CHART_ANNOTATIONS.md`](docs/CHART_ANNOTATIONS.md)。
TradingView Alert 接入、JSON 格式和默认安全边界见 [`docs/TRADINGVIEW.md`](docs/TRADINGVIEW.md)。

控制台使用本地图标资源，不依赖外部图标 CDN。同版本资源可通过
`node infra/vendor-icons.mjs` 重新获取。

只读公共 WebSocket 验收：

```bash
.venv/bin/python infra/stream-smoke.py
```

该命令分别连接公共报价和业务 K 线通道，验证 BTC/ETH 两个合约的新鲜数据，
不登录私有账户、不下单，结果写入 `outputs/public-stream-verification.json`。
后端测试中的 `test_okx_stream_transport.py` 使用真实本机套接字和带认证的
HTTP/SOCKS5 测试代理，覆盖 REST 签名、四路 WebSocket、止盈止损参数、
PushPlus 传输和重连。它不替代真实 OKX Demo 或微信送达验收。

本地开发配置 `OKX_PROXY_URL` 后，可以用下面的只读命令同时验收 OKX REST、报价和多周期
K 线是否经出站代理连通：

```bash
.venv/bin/python infra/proxy-smoke.py --timeout 45
```

结果写入权限为 `600` 的 `outputs/proxy-verification.json`，报告只包含代理协议、
连接状态和记录数量，不包含代理地址、用户名或密码。WebSocket 验收逐合约检查报价更新，
并确认 1m、15m、1H、4H 四个周期都有新鲜、有效的 K 线；任意订阅缺失或过期均不通过。
该命令不会登录私有账户或发单。

目标服务器或宝塔部署使用下面的入口。它会把验收脚本送进运行中的 API 容器，
不要求宿主机安装 API 依赖，也不会重启服务：

```bash
./infra/openperpdesk.sh proxy-smoke --timeout 45
```

服务器上还可以用容器内的真实模型做一次只读 TradingAgents 验收：

```bash
./infra/openperpdesk.sh ai-live-smoke --run-mode fast --timeout 240
# 需要完整上游多智能体辩论时：
./infra/openperpdesk.sh ai-live-smoke --run-mode full --timeout 1800
```

它读取 OKX 公共快照并调用配置的模型服务，报告只保存模型连接、证据数量和
不可执行标志，不保存模型正文、凭据或账户数据；失败时不会留下旧的成功报告。
`fast` 是单次有界模型调用，适合网页交互；`full` 会运行完整研究图，耗时和模型
消耗更高，必须单独验收，不能因为 `fast` 成功就视为 `full` 已通过。

完整交易闭环本地联调：

```bash
.venv/bin/python infra/demo-smoke.py
```

该命令在独立子进程启动真实 API，使用临时数据库和只绑定本机回环地址的
REST/WebSocket/PushPlus 测试服务。不会读取现有交易凭据或连接真实资金账户。
覆盖分析、交易所数据核验、幂等发单、成交与保护价首轮同步、原生保护触发、
两类撤单、Worker、持久急停，以及收到交易所受理后 API 被强制终止的恢复流程。
报告写入 `outputs/trading-flow-verification.json`。
本地协议测试服务不是交易所撮合引擎，结果不代表真实 OKX Demo 或微信送达已验收。

验证基线（2026-09-20，代码提交 `e310145`）：

- 本地完整 API 回归 864/864 通过，耗时 1054.539 秒。该代码提交的 PR CI
  `35513825611` 与 push CI `35513823217` 均已完成并成功，覆盖 API、Web、
  浏览器、Compose 和 TradingAgents 镜像五项任务；不代表后续提交已经通过。
- 本机协议测试覆盖 Demo 发单、成交、原生保护、Worker、急停、重复请求、
  进程中断及响应丢失恢复。浏览器样本覆盖中文操作、深浅主题和电脑/手机尺寸，
  并核对没有向测试交易所发出变更请求；这些不是实际 OKX 撮合或真实账户验收。
- TradingView 已在目标 HTTPS 入口开启只接收模式，脚本请求的持久回执、
  私有 SSE、去重和拒绝路径通过，订单为零。平台实际生成的 Alert 仍未验证。
- TradingAgents：真实图和结构化协议通过本地模型夹具；目标服务器上的真实模型
  `fast` 与 `full` 研究也已通过只读验收，均带 OKX 公共行情证据且不会授权执行。
- Docker/宝塔 HTTPS、公开实时行情、服务端 SOCKS5、同镜像重启、API 崩溃自动恢复、
  生产当前快照恢复和旧/新镜像切换均有目标环境证据。它们在没有私有交易的状态下执行，
  不能证明真实在途订单或宿主机断电恢复。
- Worker 安全补丁已上线，API 为 `release-e310145-worker-safety`、Web 为
  `release-43f98c8`。26 张表逐表摘要、配置范围、独立公网 SSE 和新异机备份核对通过。
  详细范围及 digest 见 [`2026-09-20 验收记录第 13 节`](docs/DEPLOYMENT_ACCEPTANCE_2026-09-20.md#13-自动-worker-安全补丁与维护基线更新)。

尚缺真实 OKX Demo 私有流、订单/成交/原生保护与对账、季度文件及跨日权益观测、
延长 Demo 运行、PushPlus 微信收信、TradingView 平台告警和批准维护窗口内的宿主机
断电恢复。默认继续关闭执行和自动交易，不把上述测试基线作为实盘批准。

## 目录结构

```text
apps/web/       浏览器 Web 页面
services/api/   FastAPI 后台接口
docs/           架构、安全和交付说明
docker-compose.yml
```

## 部署

生产部署前请先阅读 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)。当前 Compose
只持久化 API 的 SQLite 数据卷；Web 通过 Nginx 反代到 API。实盘默认被独立
安全闸门锁定，不能把“页面可访问”视为“交易已启用”。目标服务器构建、宝塔 HTTPS、
公开 SSE、服务端 SOCKS5 代理和真实模型研究的 2026-09-19 验收记录见
[`docs/DEPLOYMENT_ACCEPTANCE_2026-09-19.md`](docs/DEPLOYMENT_ACCEPTANCE_2026-09-19.md)；
生产重启、恢复、回滚及最新安全补丁记录见
[`docs/DEPLOYMENT_ACCEPTANCE_2026-09-20.md`](docs/DEPLOYMENT_ACCEPTANCE_2026-09-20.md)。
真实 OKX Demo 私有账户与在途交易、PushPlus 微信送达、TradingView 平台告警及
宿主机断电仍须单独验收，不能由无私有交易的恢复/回滚演练代替。

部署命令入口为 `infra/openperpdesk.sh`，主机需要 Python 3.11+ 和 Compose v2。
`preflight` 校验 Compose 最终解析配置；`backup` 生成含 WAL 提交数据的一致性快照及
SHA-256 清单；`restore` 先保存旧库，再强制以禁用执行、停用 Worker 和持久急停状态启动。
默认 Web 仅绑定 `127.0.0.1`，交给宝塔或其他 HTTPS 反向代理对外提供访问。
这些维护流程已有本地故障注入测试，但不能替代目标服务器上的真实容器演练。
配置真实 OKX Demo 凭据后，`./infra/openperpdesk.sh private-smoke` 可直接使用运行中
API 容器的连接配置做只读 REST/私有 WebSocket 验收，输出受保护的连接与记录数量报告。
此检查不下单，也不等同于真实 Demo 的成交与保护闭环验收。

完成只读验收后，可在单独批准的维护窗口执行一次最小订单生命周期验收：

```bash
./infra/openperpdesk.sh demo-lifecycle-smoke \
  --confirm OPENPERPDESK_OKX_DEMO_LIFECYCLE \
  --inst-id BTC-USDT-SWAP \
  --side long \
  --timeout 120
```

该命令只接受 Demo 模式和容器内回环 API，要求执行开关显式开启、TradingView 信号关闭、
自动 Worker 关闭且保持 Dry Run 配置。验收期间不得在其他终端操作同一合约。
它先做交易所数据预检，再发送最小张数开仓，直接核对私有
WebSocket 中匹配 `clOrdId` 的成交事件、`tradeId`、`fillSz`、`fillPx` 和原生止盈止损
业务流，随后只减仓平仓、终止残留保护单并再次对账。进入验收后，无论成功或失败都尝试
停用 Worker 并触发急停；无法验证清理或急停时明确报错，必须人工核对账户。
成功报告保存在权限为 `600` 的
`outputs/okx-demo-lifecycle-verification.json`，不包含密钥、余额或订单编号。
成功后系统仍处于急停，必须人工核对 OKX Demo 账户和本地账本后再决定是否恢复。
不得使用曾经暴露、带提现权限或未限制出口 IP 的密钥执行此命令。

生产可在 `.env` 中设置
`OPENPERPDESK_COMPOSE_OVERLAY=docker-compose.release.yml`、
`OPENPERPDESK_API_IMAGE` 和 `OPENPERPDESK_WEB_IMAGE` 固定已验收的 release
镜像。部署脚本会自动跳过 `--build`，后续 `up`、`restart`、备份和恢复沿用同一
镜像选择；开发环境不设置覆盖文件时仍使用源码构建。

## 开源协议

MIT，详见 `LICENSE`。

本软件用于研究和自动化工程，不构成投资建议，也不承诺任何收益。
