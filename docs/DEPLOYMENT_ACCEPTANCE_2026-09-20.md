# OpenPerpDesk 生产同镜像重启验收（2026-09-20）

本次在阿里云上海目标服务器验证部署脚本 `4c0e2a4` 的真实重启行为。
只更新 `infra/deploy.py` 和 `infra/okx-demo-lifecycle-smoke.py`；
没有构建或替换应用镜像，没有配置私有账户，也没有执行交易。

## 1. 修复与范围

旧 `restart` 与 `up` 使用相同的 Compose 命令，配置和镜像未变化时可能不重启。
现在 `restart` 明确传递 `--force-recreate`，等待健康检查并刷新容器内 Nginx；
普通 `up` 不强制重建，release 模式继续使用已指定的镜像。

本次为会短暂断开连接的受控维护，不是无中断升级，也不是旧镜像回滚或断电恢复。
启动前确认 AI 研究空闲、私有凭据未配置、Worker 和所有执行入口关闭。

## 2. 备份与身份

- 项目目录：`/opt/openperpdesk`
- Compose 项目：`openperpdesk`，只有 `api`、`web` 两个服务
- Web 绑定：`127.0.0.1:18099`，公网入口 `https://okx.dalongxia.com.cn/`
- API 镜像：`openperpdesk-api:release-71fccf3-tradingagents-v4`
- API 镜像摘要：`sha256:c0d140b3446a1cab78f56d48342337ce6f0c5b832677a5ceed8c3437e8e7ad62`
- Web 镜像：`openperpdesk-web:release-c37f5d9`
- Web 镜像摘要：`sha256:14bf71e6ec1ee1d90a2b30145b5e6ef62b96331bb0f79519017338f7aea41bf9`
- `.env` 和部署脚本备份：`/opt/openperpdesk/releases/source-backups/restart-20260920-4c0e2a4/`
- 数据库快照：`openperpdesk-20260920T081727Z-86735951.sqlite3`，1,351,680 字节
- 快照 SHA-256：`cc963a9cfcdd1d0289fbcc320c63b40ec860d03a22c570997e50eaa515b7cfe1`
- 验收证据：`/opt/openperpdesk/outputs/restart-acceptance-20260920-4c0e2a4.json`，权限 `600`

旧部署脚本摘要与 `c37f5d9` 中的文件一致，未覆盖未知的服务器修改。
新部署脚本和生命周期工具分别通过 SHA-256 核对后才执行重启。
生命周期工具只部署到服务器，本次没有运行其交易命令。

## 3. 实际重启与数据保留

| 服务 | 重启前容器 | 重启后容器 | 新容器启动时间（UTC） |
| --- | --- | --- | --- |
| API | `6a2fcc2fd0f8` | `9077016e1782` | 2026-09-20 08:18:37 |
| Web | `0d67956894d1` | `9466287ecca01` | 2026-09-20 08:18:49 |

两个容器均恢复 `healthy`。镜像 ID、镜像标签、挂载数据卷、`.env` 文件摘要和
宝塔目标站点配置摘要在重启前后完全相同，没有重启宿主机或修改全局 Nginx。

逐表行数和内容摘要核对通过：

- 56 份研究报告。
- 1 套策略。
- 3 条图表标记。
- 持久控制标记、订单、成交、持仓、TradingView 告警及三类保护维护记录。

订单、成交和持仓均为零，所以本次不能证明真实交易数据对账或未知提交恢复。
受检查的已有 PM2 进程身份、状态和重启计数未变化；同机站点的 HTTP 结果也未变化。
其中一站在维护前后均返回 403，不能将这个基线结果描述为该站健康验收通过。

## 4. 重启后验证

- `./infra/openperpdesk.sh smoke` 通过 Web、图标、健康、readiness 和管理员鉴权检查。
- 未认证的私有 SSE 返回 HTTP 401。
- 公网系统 SSE 首包约 0.147 秒，后续心跳间隔约 5.060 秒。
- 公网行情 SSE 首包约 0.079 秒，后续行情事件间隔约 0.239 秒。
- `app.database_maintenance verify-runtime` 确认执行关闭且持久急停有效。
- TradingAgents 运行环境自检返回 `runtime_state=ready`、`execution_authorized=false`。
  此检查没有调用模型完成研究，`provider_connection_verified=false`；真实模型研究证据
  仍以 2026-09-19 的验收记录为准。

维护前通过管理员接口激活急停，重启后继续保留。以下配置没有放宽：

```dotenv
TRADING_MODE=demo
OKX_DEMO=true
EXECUTION_ENABLED=false
LIVE_TRADING_ENABLED=false
AUTO_TRADING_ENABLED=false
AUTO_TRADING_DRY_RUN=true
TRADINGVIEW_ENABLED=false
```

## 5. 测试与未完成项

本地全量 API 回归 860/860（1041.029 秒）、部署专项测试 55/55、
Node 实时与外观测试 13/13 通过。
修复提交 `4c0e2a4` 的 push CI `35498712709` 和 PR CI `35498715050` 均已通过
Compose 真实容器、浏览器、Web 和 TradingAgents 镜像任务。
Compose 验证覆盖源码与预构建镜像两条路径，并断言容器 ID 改变、数据库标记与急停
保留、重启后的 SSE 可访问。全量 API 回归的最终状态以对应 CI 为准。

本次完成的是生产同镜像重启。以下事项仍未完成：

- 生产备份实际恢复切换、旧镜像回滚和断电恢复。
- 全新最小权限 Demo 凭据下的私有 WS、订单、成交及原生保护验收。
- PushPlus 微信收信、真实 TradingView Alert、真实账单和权益边界核对。
- 延长 Demo 观察及任何真实资金试运行。
