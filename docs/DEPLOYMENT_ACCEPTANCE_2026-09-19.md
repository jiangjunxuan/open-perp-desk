# OpenPerpDesk 目标服务器部署验收（2026-09-19）

本文记录提交 `71fccf32c41b644ac06756f4b491d55f465d949c` 在阿里云上海目标服务器上的
只读、模拟盘部署验收。本文不是实盘批准，也不证明 OKX 私有账户、真实成交或微信送达已完成。

## 1. 部署身份

- 公网入口：`https://okx.dalongxia.com.cn/`
- API 镜像：`openperpdesk-api:release-71fccf3-tradingagents-v4`
- API 镜像 digest：`sha256:c0d140b3446a1cab78f56d48342337ce6f0c5b832677a5ceed8c3437e8e7ad62`
- Web 镜像：`openperpdesk-web:release-71fccf3`
- Web 镜像 digest：`sha256:9b552beecf2ea1a7485ab53acda42552bad6c86847a0e0aede03528321546d60`
- API 与 Web 容器均为 `healthy`。
- `.env` 权限为 `600`；`./infra/openperpdesk.sh preflight` 通过，输出不包含密钥。
- 公网 `index.html` 及 7 个 Web 静态资源的 SHA-256 与本提交逐一一致。

GitHub Actions 的 push 与 pull request 两次 CI 均通过，`api`、`web`、`browser`、
`compose` 和 `tradingagents-image` 五个 job 全部成功：

- [push CI 35446452636](https://github.com/jiangjunxuan/open-perp-desk/actions/runs/35446452636)
- [pull request CI 35446455150](https://github.com/jiangjunxuan/open-perp-desk/actions/runs/35446455150)

## 2. 安全状态

目标容器在验收时保持：

```dotenv
TRADING_MODE=demo
OKX_DEMO=true
EXECUTION_ENABLED=false
LIVE_TRADING_ENABLED=false
AUTO_TRADING_ENABLED=false
AUTO_TRADING_DRY_RUN=true
TRADINGVIEW_ENABLED=false
```

公开状态接口同时确认：

- `live_orders_allowed=false`
- 自动策略 Worker 未启用且保持 Dry Run
- OKX 私有凭据、私有账户流和算法订单流未配置
- PushPlus 未配置
- TradingView 接收与执行未启用

本次验收没有登录私有账户、没有提交订单，也没有使用真实资金。

## 3. HTTPS、实时推送与界面

- `/api/v1/health` 返回 `status=ok`。
- `/api/v1/health/readiness` 返回 HTTP 200、`ready=true`。
- 公网系统 SSE 首个心跳约 `0.168s` 到达，后续心跳间隔约 `5.057s`。
- 公网行情 SSE 首个事件约 `0.061s` 到达，后续事件间隔约 `0.243s`。
- 未认证私有 SSE 返回 HTTP 401。
- 7 个主视口完成公开浏览器验收，页面级横向溢出和浏览器错误均为 0。
- 主要路由、研究模式选择、图表标记、TradingView 管理界面和保护事故复核流程均完成浏览器检查。

本地验收产物保存在未提交的 `work/public-ui-71fccf3/`，汇总文件为
`work/public-ui-71fccf3/ui-verification.json`。

## 4. TradingAgents 真实模型验收

目标 API 容器通过 OpenAI-compatible 模型服务完成两种只读研究模式：

| 模式 | 完成时间（UTC） | 耗时 | 结果 |
| --- | --- | ---: | --- |
| `fast` | 2026-09-19T15:31:05Z | 34.83 秒 | 通过 |
| `full` | 2026-09-19T14:56:42Z | 829.94 秒 | 通过 |

两次验收都读取 `BTC-USDT-SWAP` 的 OKX 公共 ticker、100 根 `15m` K 线、资金费率和
持仓量。完整模式返回新闻、基本面、情绪、投资辩论、风险辩论和最终决策等研究状态。

两次报告都明确记录：

```text
provider_connection_verified=true
execution_authorized=false
private_account_verified=false
trading_performed=false
```

这证明模型连接和只读研究链可用，不证明模型结论正确，也不允许 AI 绕过结构化信号、
风控、幂等执行和安全闸门。

## 5. 出站代理验收

目标服务器在 2026-09-19T15:31:45Z 重新完成服务端 `socks5h` 只读代理验收：

- BTC/ETH 永续合约 REST ticker 与 K 线均通过。
- 公共报价 WebSocket 已连接、持续更新且数据新鲜。
- `1m`、`15m`、`1H`、`4H` K 线 WebSocket 均已连接且数据新鲜。
- 报告不保存代理地址、用户名或密码。
- `private_account_verified=false`、`trading_performed=false`。

PushPlus 尚未配置，因此本次不包含 PushPlus 经代理送达的真实验收。

## 6. 备份与回滚证据

- 数据库快照：`openperpdesk-20260919T140256Z-ab9a8189.sqlite3`
- SHA-256：`0c7fb1523bd101a2acb3671bc330f8c9ed750e0515a76acce13568176304347e`
- 升级前源码归档：`20260919T142417Z-pre-71fccf3`

备份文件、校验值和升级前源码均已确认存在。目标服务器上的实际恢复、旧镜像切换、
回滚后重新核对和断电恢复尚未演练，不能把“已有备份”表述为“回滚已验证”。

## 7. 测试证据

- 本次功能专项 Python 测试：71/71 通过。
- Node 实时与外观测试：13/13 通过。
- 同一工作树较早的完整 API 回归：839/839 通过。
- 两次 GitHub Actions CI 均通过当前提交的容器、浏览器、Web、API 和 TradingAgents 镜像任务。

## 8. 尚未完成

- OKX Demo 最小权限私有凭据、私有 REST/WS、订单、成交、持仓和原生保护对账。
- 真实 Demo 小额订单的提交、成交、撤单、故障恢复和幂等闭环。
- PushPlus token、接口受理和微信端人工收信确认。
- 真实 TradingView Alert 经 HTTPS 到 OKX Demo 的端到端验收。
- 真实季度文件、跨午夜权益、出入金和账单覆盖边界核对。
- 目标服务器实际恢复/回滚切换、断电恢复和延长模拟盘观察。
- 任何真实资金试运行。

结论：目标服务器的 Web、HTTPS、公开实时行情、只读代理、TradingAgents 真实模型研究、
容器运行和部署前备份已验收；私有 Demo 交易闭环与外部通知仍未验收，实盘继续锁定。
