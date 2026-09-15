# TradingView 信号接入

OpenPerpDesk 将 TradingView 作为看盘和策略信号来源，TradingView 不直接持有
OKX API 密钥，也不绕过后台风控。Alert 进入服务器后会依次经过：

```text
TradingView Alert
  -> Webhook 密钥和 JSON 校验
  -> 合约名称标准化与白名单
  -> TradeSignal 有效期校验、持久化收件箱
  -> HTTP 202 接收回执
  -> 异步处理进程
  -> 风控引擎
  -> 幂等执行器
  -> OKX Demo REST/WebSocket
```

## 服务器配置

在服务器受保护的 `.env` 中设置：

```dotenv
TRADINGVIEW_ENABLED=true
TRADINGVIEW_WEBHOOK_SECRET=请使用随机长字符串
TRADINGVIEW_EXECUTION_ENABLED=false
TRADINGVIEW_DRY_RUN=true
TRADINGVIEW_SYMBOLS=BTC-USDT-SWAP,ETH-USDT-SWAP
```

默认只做预览。确认模拟盘环境、OKX Demo 凭据、私有 WebSocket、账户对账和
PushPlus 均已验证后，才可以显式设置：

```dotenv
TRADINGVIEW_EXECUTION_ENABLED=true
TRADINGVIEW_DRY_RUN=false
EXECUTION_ENABLED=true
TRADING_MODE=demo
OKX_DEMO=true
```

Docker Compose 会显式向 API 容器传递全部 `TRADINGVIEW_*` 配置；修改 `.env`
后必须重新创建 API 容器，仅重启旧容器不会更新环境变量。先运行
`./infra/openperpdesk.sh preflight`，再通过 `./infra/openperpdesk.sh up` 更新。
生产和预发布环境的 Webhook 密钥至少 32 个字符，所有环境最多 256 个字符；
预检不会打印密钥。预检同时检查开关、合约白名单、信号时效和数值参数。
显式空白名单不会被 Compose 替换为默认合约；Dry Run 只有明确配置为
`false` 才能解除，空值或拼写错误即使跳过部署预检也保持预览模式。

实盘不会因为 TradingView 配置而自动放行，仍需满足项目独立的实盘配置、人工
解锁和急停闸门。

## Webhook 地址

```text
https://你的域名/api/v1/integrations/tradingview/webhook
```

TradingView Alert 的消息使用 JSON。`timestamp`（带时区）和 `alert_id`
现在为必填项。将 `{{timenow}}` 放进 `timestamp`，并让每个策略、动作和
告警事件有独立的 `alert_id`，同一投递的重试必须保持 ID 与内容不变。
以下固定价位仅演示字段，不能直接用于实际交易：

```json
{
  "secret": "与 TRADINGVIEW_WEBHOOK_SECRET 相同",
  "alert_id": "strategy1-long-{{exchange}}-{{ticker}}-{{time}}-{{timenow}}",
  "symbol": "{{exchange}}:{{ticker}}",
  "timestamp": "{{timenow}}",
  "action": "open_long",
  "confidence": 0.9,
  "leverage": 2,
  "position_pct": 5,
  "entry_price": 50000,
  "stop_loss": 49000,
  "take_profit": 52000,
  "size": 1
}
```

`open_short` 要求价格满足 `take_profit < entry_price < stop_loss`。
`close` 必须提供 `side`（`buy` 或 `sell`），避免在双向持仓下错误平仓。
开仓方向由 `open_long` / `open_short` 唯一决定；若额外提供的 `side`
与动作矛盾则直接拒绝，同义方向不影响去重。
符号支持 `BTC-USDT-SWAP`、`BTCUSDT` 和常见的 `BINANCE:BTCUSDT.P` 形式，
最终都会转换为 OKX 永续合约名称，并且必须在 `TRADINGVIEW_SYMBOLS` 白名单中。

TradingView 的 Alert 编辑器使用 JSON 中的 `secret`。`X-TradingView-Token`
请求头是供自建桥接客户端使用的替代方式，不代表 TradingView 编辑器支持
自定义请求头。系统不会保存密钥、未知 JSON 字段、原始 Webhook 内容或
OKX 凭据；收件箱只保存规范化指令、非敏感执行结果与账户身份摘要。

控制台“风控与连接 / TradingView 信号接入”提供地址、只读测试模板和
最近 30 条告警。测试模板固定 `hold`、`dry_run=true`，只确认接收，不下单。
告警变化随私有 SSE 推送，管理员访问失效或推送断开后清除页面记录。

TradingView 官方要求 Webhook 服务在三秒内响应，且只支持 80/443 端口。
因此 HTTP 请求仅做校验和 SQLite 持久化，不等待交易所预检、下单或微信通知；
目标部署必须提供公网可达的 HTTPS 443 入口，本机 8099 不是 TradingView 可达地址。
参见 TradingView 官方文档：
<https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/>

## 回执与处理状态

`202` 的 `accepted=true` 仅表示**告警已持久化接收**，不是订单已获批。
`execution_accepted` 在排队时为 `null`，完成风控后才有布尔结果。
通过已认证的 `GET /api/v1/integrations/tradingview/alerts` 或私有 SSE
事件 `tradingview_alerts` 获取后续状态：

| 状态 | 含义 |
| --- | --- |
| `queued` / `processing` | 已持久化排队 / 正在处理 |
| `observed` | 观望或接收测试，不调用下单执行器 |
| `preview` | 只通过风控预览 |
| `submitted` | 委托已受理，不代表已成交 |
| `rejected` / `expired` | 未通过执行条件 / 已过期 |
| `interrupted` / `unconfirmed` | 进程中断 / 执行结果不确定，禁止自动重发，需先查单 |

已存在订单仍处于提交待确认状态时，回执保持 `unconfirmed`，
`execution_accepted` 为 `null`，不会因本次未重复下单而标成拒绝。
执行异常后优先核对已落盘订单：明确拒单记为 `rejected`；已有受理或成交证据
记为 `submitted`；没有确定证据才保留 `unconfirmed`。回执不包含上游异常正文。
此处是该次告警处理的结果，后续成交、撤销和对账结果以订单账本为准。

排队任务在重启后继续处理，但仍检查原始有效期、账户/模拟盘身份和当前
白名单。处理中的任务如进程退出，最迟在 120 秒处理租期结束后变成
`interrupted`，不会重放；已有订单通过原有订单对账链路恢复。
处理预算为 60 秒。已接收的 Dry Run 不会因配置放宽而升级为下单。
下单请求在入队时就分配客户端订单号，处理中断后仍可用来查单；
号码存在本身不代表交易所已接收到委托。
数据库恢复会把备份中仍在排队/处理的告警改为 `interrupted`，以免执行
已经在备份之后成交的旧指令；已完成记录保留。此行为独立于急停和执行开关。

## 响应和安全边界

- 重复的 `alert_id` 返回同一接收记录，不会重新排队；参数不同则返回 `409`。
- Alert 超过 `TRADINGVIEW_MAX_AGE_SECONDS` 会被拒绝。
- 客户端不能延长服务端有效期；排队时长不会刷新告警寿命。
- 请求体最多 16 KiB，重复 JSON 键、非有限数值和空白名单均拒绝。
- 排队/处理中最多 1000 条，容量耗尽返回 `429`；数据库忙返回 `503`，未返回
  `202` 不代表已接收，重试必须使用原 ID/内容而非新建下单指令。
- 没有止损和止盈的开仓信号不会进入执行器。
- TradingView 的 `dry_run` 只能增加限制，不能绕过服务器的 Dry Run 配置。
- 最终成交状态以 OKX 私有 WebSocket 和 REST 对账为准。
- 生产环境应只通过 HTTPS 反向代理暴露该入口，并在代理层限制请求体大小和
  访问频率。接收回执性能和外部可达性仍须在实际 HTTPS 部署环境验收。
