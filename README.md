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

当前仓库已经包含可运行的 Demo 交易闭环，但仍不是可直接投入真实资金的成品：

- 默认模式为 `demo` 模拟盘
- 尚未启用实盘下单
- 仓库不保存任何 API 密钥
- Web 页面可读取公开行情和不敏感的系统状态
- 私有账户总览需要 `X-Admin-Token`；账户适配器已实现但默认未配置密钥
- 私有账户 WebSocket 已实现基础登录和账户、持仓、订单事件缓存
- 原生止盈止损的 `orders-algo` 业务 WebSocket 已接入，算法订单会进入本地订单账本
- 模拟盘订单客户端、签名、幂等执行和执行闸门已实现；默认仍不会下单
- 结构化策略、风控预检、Demo 订单预览、止盈止损参数和本地保护兜底已实现
- 自动策略 Worker、历史回测、急停/恢复和审计落盘已实现，自动交易默认关闭
- 私有 REST 对账包含持仓、pending/history 订单和 fills-history，并会关闭交易所快照中已消失的本地仓位
- PushPlus 和 TradingAgents 都是可选集成，未配置时不会影响结构化策略
- 实盘有独立安全闸门，必须满足多项配置并由管理员在进程内手动解锁，服务重启后自动回锁

在交易执行、持仓校验、风险控制和异常场景测试完成前，不要使用真实资金。

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

## 当前功能范围

- 实时行情、K 线图和合约选择
- 永续合约账户、持仓、订单和运行日志同步
- 结构化策略分析、信号有效期和风控评估
- 历史回测、Demo 预览和信号执行
- 风控限额、原生止盈止损、保护性止盈止损和紧急停止
- 自动策略 Worker，默认关闭且默认只做 Dry Run
- OKX REST/WebSocket 连接，以及可选的 SOCKS5/HTTP 出站代理
- OKX 私有账户流与业务算法订单流，分别用于账户状态和原生止盈止损状态
- PushPlus 通知客户端和测试接口
- TradingAgents 可选适配器
- 成交历史同步、已实现 PnL、手续费和净 PnL 汇总
- 性能报告：收益率、峰值回撤、日汇总、策略汇总和权益曲线
- SQLite 本地状态落盘和 Docker Compose 部署骨架
- Docker、宝塔反向代理、HTTPS、服务端 SOCKS5/HTTP 代理、SQLite 备份恢复说明见
  [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- TradingAgents 需要额外依赖时，可使用 `docker-compose.tradingagents.yml` 构建可选 API 镜像

界面设计基线见 [`docs/DESIGN_SYSTEM.md`](docs/DESIGN_SYSTEM.md)，当前 Web 控制台为中文，设计方向是高密度、低干扰的深色交易后台。

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
安全闸门锁定，不能把“页面可访问”视为“交易已启用”。

## 开源协议

MIT，详见 `LICENSE`。

本软件用于研究和自动化工程，不构成投资建议，也不承诺任何收益。
