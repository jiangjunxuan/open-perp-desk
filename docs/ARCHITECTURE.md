# 系统架构

## 运行边界

浏览器只是控制和监控界面，不是交易引擎。关闭浏览器后，交易 Worker 仍应能够
继续运行；任何未认证客户端的命令都不能被信任。

```text
浏览器
  |
  v
反向代理 / HTTPS
  |
  +--> Web 前端
  +--> 后台 API
              |       (同一容器内的生命周期任务)
              |             +--> 公共行情 WebSocket
              |             +--> 账户 WebSocket（只读）
              |             +--> 业务 WebSocket（只读，orders-algo）
              |             +--> 账户对账器 ---> OKX 私有 REST（只读）
              |             +--> 自动策略 Worker
              |
              +--> SQLite（DATA_DIR 持久卷）
              |
              +--> 分析服务 / 风控引擎 / 订单执行器 ---> OKX REST
                                                              ^
                                                              |
                                                    可选 SOCKS5/HTTP 出站代理
```

## 订单生命周期

1. 公共行情 WebSocket 缓存 ticker 和 1 分钟 K 线，REST 补齐所选周期和历史窗口。
2. 私有 WebSocket（配置凭据时）缓存账户、持仓和普通订单事件；业务 WebSocket
   额外缓存原生止盈止损的 `orders-algo` 事件；账户对账器定时用 REST 快照校正。
3. 结构化策略和可选 TradingAgents 生成研究结果；只有结构化 `TradeSignal` 可以进入执行链。
4. 风控引擎检查服务端行情流新鲜度、信号有效期、敞口、杠杆、亏损限额和重复订单。
5. 只有风控批准后，执行器才允许进入 Demo 预览或 Demo 下单；直接订单入口被关闭。
6. 订单、成交、持仓、分析和审计事件写入 SQLite，并通过客户端订单 ID 做幂等控制。
7. 通过私有 WebSocket 事件、fills-history 和 pending orders REST 对账确认状态；止盈止损同时保留交易所原生保护和本地保护兜底。
8. PushPlus 只发送已配置的操作事件；通知失败记录为告警，不改变订单结果。

余额、持仓、订单、成交和风险限额不能只以 AI 层的结果为准。

## 初始技术选择

- FastAPI：后台 API
- Nginx：提供浏览器 Web 页面
- Docker Compose：本地和服务器部署
- SQLite：订单、成交、持仓、策略、分析、控制标记和审计事件持久化
- FastAPI lifespan task：公共行情、私有账户流、账户对账和自动 Worker
- 模拟盘：默认交易环境，实盘由独立进程内安全闸门额外保护
- Worker 支持管理员令牌保护的运行时启停；进程重启后仍回到环境变量默认状态
- 健康层区分进程存活、交易前置条件 readiness 和不含密钥的运行指标

## 失败关闭边界

- 未配置管理员令牌时，所有私有账户、对账、策略保存、风控和执行命令拒绝访问。
- 未配置 OKX 凭据时，账户同步器和私有 WebSocket 不启动。
- `EXECUTION_ENABLED=false`、`AUTO_TRADING_ENABLED=false` 和 `AUTO_TRADING_DRY_RUN=true` 是默认值。
- 急停标记写入 SQLite，所有新的信号执行和 Worker 周期都必须先检查。
- 实盘必须同时满足实盘模式、独立配置开关、执行开关和进程内人工解锁；进程重启后自动回锁。
- 浏览器关闭不会停止 Worker，但浏览器本身不持有交易状态，也不能绕过服务端认证。
