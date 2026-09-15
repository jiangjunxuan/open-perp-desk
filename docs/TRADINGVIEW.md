# TradingView 信号接入

OpenPerpDesk 将 TradingView 作为看盘和策略信号来源，TradingView 不直接持有
OKX API 密钥，也不绕过后台风控。Alert 进入服务器后会依次经过：

```text
TradingView Alert
  -> Webhook 密钥和 JSON 校验
  -> 合约名称标准化与白名单
  -> TradeSignal 有效期校验
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

实盘不会因为 TradingView 配置而自动放行，仍需满足项目独立的实盘配置、人工
解锁和急停闸门。

## Webhook 地址

```text
https://你的域名/api/v1/integrations/tradingview/webhook
```

TradingView Alert 的消息使用 JSON。推荐将 `{{timenow}}` 放进 `timestamp`，
并使用唯一的 `alert_id`：

```json
{
  "secret": "与 TRADINGVIEW_WEBHOOK_SECRET 相同",
  "alert_id": "{{exchange}}-{{ticker}}-{{time}}-{{timenow}}",
  "symbol": "{{exchange}}:{{ticker}}",
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
符号支持 `BTC-USDT-SWAP`、`BTCUSDT` 和常见的 `BINANCE:BTCUSDT.P` 形式，
最终都会转换为 OKX 永续合约名称，并且必须在 `TRADINGVIEW_SYMBOLS` 白名单中。

也可以把密钥放在 `X-TradingView-Token` 请求头中。系统不会把密钥、原始
Webhook 内容或 OKX 凭据写入审计日志。

## 响应和安全边界

- 重复的 `alert_id` 使用同一个幂等键，不会重复提交订单。
- Alert 超过 `TRADINGVIEW_MAX_AGE_SECONDS` 会被拒绝。
- 没有止损和止盈的开仓信号不会进入执行器。
- TradingView 的 `dry_run` 只能增加限制，不能绕过服务器的 Dry Run 配置。
- Webhook 的返回结果只说明服务器是否受理，不代表交易所已经成交；成交状态
  以 OKX 私有 WebSocket 和 REST 对账为准。
- 生产环境应只通过 HTTPS 反向代理暴露该入口，并在代理层限制请求体大小和
 访问频率。
