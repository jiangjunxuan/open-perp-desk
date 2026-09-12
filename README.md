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

当前仓库是安全的初始骨架：

- 默认模式为 `demo` 模拟盘
- 尚未启用实盘下单
- 仓库不保存任何 API 密钥
- Web 页面可读取公开行情和不敏感的系统状态
- 私有账户总览需要 `X-Admin-Token`；账户适配器已实现但默认未配置密钥
- 自动交易 Worker 暂未连接交易所

在交易执行、持仓校验、风险控制和异常场景测试完成前，不要使用真实资金。

## 本地启动

```bash
cp .env.example .env
docker compose up --build
```

浏览器打开 `http://localhost:8080`。

API 健康检查地址为 `/api/v1/health`。

只读市场接口包括 `/api/v1/market/ticker`、`/api/v1/market/candles`、
`/api/v1/market/overview` 和 `/api/v1/market/stream`。私有账户总览为
`/api/v1/account/overview`，需要 `X-Admin-Token`。

## 计划实现的功能

- 实时行情、自选列表和图表
- 永续合约持仓、订单、保证金、杠杆和盈亏
- AI 研究报告和结构化交易信号
- 回测和策略对比
- 风控限额、止盈、止损和紧急停止
- 实盘前先运行模拟盘
- OKX REST/WebSocket 连接，以及可选的 SOCKS5/HTTP 出站代理
- 信号、成交、风险事件和系统故障的 PushPlus 通知
- 审计日志、账户权限和 Docker 部署

## 目录结构

```text
apps/web/       浏览器 Web 页面
services/api/   FastAPI 后台接口
docs/           架构、安全和交付说明
docker-compose.yml
```

## 开源协议

MIT，详见 `LICENSE`。

本软件用于研究和自动化工程，不构成投资建议，也不承诺任何收益。
