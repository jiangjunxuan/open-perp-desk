# OpenPerpDesk

OpenPerpDesk 是一个开源的 AI 辅助永续合约交易后台。它运行在服务器上，
通过浏览器访问，并使用 Docker 部署。

计划整合：

- TradingAgents：研究分析和多智能体市场研判
- OKX 官方 agent skills 或 API 适配层：行情和交易能力
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
目前仍需用真实 OKX Demo 文件完成季度归档端到端验收，完善统一币种的完整绩效和目标服务器断线与断电恢复演练，
并完成真实 Demo 和目标服务器验收。详见 `docs/ROADMAP.md`。

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
- SQLite 本地状态落盘和 Docker Compose 部署骨架
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
`google-chrome`，其他位置可通过 `CHROME_BIN` 指定。它验证 1440/1024/844 横屏/768/390/375/320px
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

本次工作树验证结果：

- 上一提交基线 `0e53e18`：API 单元和集成测试 751 项通过。
- TradingView 专项现有 31 项通过：快速接收、持久化排队、异步风控执行、
  并发去重、参数冲突、过期、跨账户拒绝、进程崩溃不重发与私有 SSE。
  数据库备份恢复专项 17 项通过，包含恢复后不重放旧告警。
- 中文告警面板：10 项交互检查、深浅色共 10 组尺寸检查通过，页面不展示密钥；
  本机协议浏览器验收无交易所变更请求。这不代表真实 TradingView 告警已验收。
- Demo 交易闭环：9 个场景全部通过，覆盖幂等发单、成交、原生保护、
  Worker、急停、进程崩溃恢复和禁止重复发单。
- TradingAgents：真实图和结构化协议通过本地模型夹具，11 次调用均带
  OKX 公共行情证据；真实模型服务仍未验证，且不会授权执行。
- 实时 SSE：市场事件约 250ms 更新、系统心跳约 5 秒、私有未认证请求返回
  401；断流时页面清空当前旧快照并锁定执行。
- Docker/宝塔、真实 OKX 私有流、PushPlus 微信送达和目标服务器验收仍需在
  外部环境完成。

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
安全闸门锁定，不能把“页面可访问”视为“交易已启用”。真实 OKX Demo 私有
WebSocket、订单状态/成交回报、PushPlus token、服务器构建、宝塔 HTTPS 和回滚
仍需要在目标环境逐项验收。

部署命令入口为 `infra/openperpdesk.sh`，主机需要 Python 3.11+ 和 Compose v2。
`preflight` 校验 Compose 最终解析配置；`backup` 生成含 WAL 提交数据的一致性快照及
SHA-256 清单；`restore` 先保存旧库，再强制以禁用执行、停用 Worker 和持久急停状态启动。
默认 Web 仅绑定 `127.0.0.1`，交给宝塔或其他 HTTPS 反向代理对外提供访问。
这些维护流程已有本地故障注入测试，但不能替代目标服务器上的真实容器演练。
配置真实 OKX Demo 凭据后，`./infra/openperpdesk.sh private-smoke` 可直接使用运行中
API 容器的连接配置做只读 REST/私有 WebSocket 验收，输出受保护的连接与记录数量报告。
此检查不下单，也不等同于真实 Demo 的成交与保护闭环验收。

## 开源协议

MIT，详见 `LICENSE`。

本软件用于研究和自动化工程，不构成投资建议，也不承诺任何收益。
