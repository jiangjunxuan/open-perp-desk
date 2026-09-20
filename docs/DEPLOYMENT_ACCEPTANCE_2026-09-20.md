# OpenPerpDesk 生产重启与恢复验收（2026-09-20）

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

以上同镜像重启记录不包含下列事项；后续进程故障和当前快照恢复分别见第 6、7 节：

- 生产备份实际恢复切换、旧镜像回滚和断电恢复。
- 全新最小权限 Demo 凭据下的私有 WS、订单、成交及原生保护验收。
- PushPlus 微信收信、真实 TradingView Alert、真实账单和权益边界核对。
- 延长 Demo 观察及任何真实资金试运行。

## 6. API 进程崩溃后的自动恢复

2026-09-20 08:34:45 UTC，在再次确认研究空闲、无私有凭据、执行关闭和持久急停后，
只对 API 容器内的 Uvicorn 子进程发送一次 `SIGKILL`。未停止 Docker daemon，
未终止宿主机其他服务，也未使用 `docker restart` 或重新执行 Compose 启动来帮助恢复。

- 故障前快照：`openperpdesk-20260920T083442Z-5c5e1bca.sqlite3`。
- 快照 SHA-256：`cc963a9cfcdd1d0289fbcc320c63b40ec860d03a22c570997e50eaa515b7cfe1`。
- API 容器保持 `9077016e1782`，Docker `RestartCount` 从 0 变为 1。
- Docker 事件依次记录 `die`（退出码 137）与 `start`；没有手动停止或重启事件。
- 新 API 进程启动于 `2026-09-20T08:34:46.4905263Z`，随后恢复健康。
- 容器镜像、挂载、配置文件和宝塔站点配置均未改变，Web 容器也没有重启。
- 56 份报告、1 套策略、3 条标记及其他受核对表的内容摘要一致；持久急停仍有效。
- 受检查的 PM2 进程和同机站点响应保持原基线状态。

演练期间持续运行与生产 `/live.js` 内容摘要一致的 `openLiveStream` 客户端，
通过真实公网 HTTPS 同时订阅行情和系统流。没有替换 `fetch`、缩短退避或模拟事件，
没有在故障后重新创建订阅来代替自动重连。

| 推送流 | 观测到的失联至恢复时间 |
| --- | --- |
| 行情流的新鲜行情事件 | 7.171 秒 |
| 系统流的新心跳 | 7.173 秒 |

两路均经历 `open -> offline -> connecting -> open`，期间的失败请求由客户端退避重试。
这验证了网页实际使用的推送客户端代码和公网链路，不是一次新的浏览器渲染/视觉验收，
也不证明私有账户推送或未确认交易已经恢复。

故障后的独立公网 SSE 检查通过：行情事件间隔约 0.253 秒、系统心跳约 5.052 秒，
未认证私有流仍返回 401。证据位置：

- 服务器：`/opt/openperpdesk/outputs/crash-acceptance-20260920.json`，权限 `600`。
- 本地：`work/deployment/crash-stream-verification-20260920.json`，权限 `600`。

CI 的 Compose 演练已加入同类 API 子进程终止、自动重启计数与进程身份核对，
以及恢复后的持久急停、数据库标记和 SSE 验证。CI 结果仍须按对应提交单独核对。
以上仅为应用进程异常退出恢复，不包含宿主机断电、生产旧镜像回滚、真实 Demo 在途订单
恢复或备份恢复切换。后续生产当前快照恢复记录见第 7 节。

## 7. 生产当前快照的实际恢复

2026-09-20 08:59:52 至 09:00:26 UTC，在生产 Compose 项目中完成当前快照的实际恢复。
先生成在线备份，确认研究空闲、无其他运行容器写入数据卷，再停止 API 并生成新的停写快照；
恢复使用的是这个新快照，没有倒回更早的研究历史。操作持有项目部署锁。

- 恢复源：`openperpdesk-offline-20260920T085953Z-8275bc69.sqlite3`。
- 恢复源 SHA-256：`cc963a9cfcdd1d0289fbcc320c63b40ec860d03a22c570997e50eaa515b7cfe1`。
- 经过宿主机 SQLite 一致性规范化后的传输 SHA-256：
  `ada9260641bc60713d803e152498f1d35d5b8cf7cc5524deb3e185bbc34cef7f`。
- 恢复前原库及回滚快照：`/data/recovery/before-20260920T085958-9fcbc6adbc/`。
- `rollback.sqlite3` 通过 SQLite 校验，SHA-256 与恢复源一致。
- 恢复后另存快照：`openperpdesk-20260920T090023Z-b95ed423.sqlite3`。
- 服务器证据：`/opt/openperpdesk/outputs/restore-acceptance-20260920.json`，权限 `600`。

调用的是现有 `Deployment.restore` 和数据库维护流程，额外记录实际传输快照的摘要，
未替换恢复逻辑。源快照与传输快照分开记录，不把 SQLite 文件字节相同作为唯一的数据完整性依据。

恢复后的比对覆盖 25 组表摘要及原有 118 条审计记录：

- 56 份报告、1 套策略参数、3 条标记，以及其他普通数据表内容保留。
- 非审计自增编号保留，原有审计记录的 ID 和内容摘要全部一致。
- 策略的启用状态与更新时间、急停记录及新增恢复审计作为预期安全变更单独核对。
  本次策略启用数在恢复前后均为 0，恢复后急停为真。
- 新增 `database_restored` 审计对应本次归档路径和实际传输 SHA-256。
- API 新容器为 `e647abe95cda`，Web 新容器为 `da294c3d3335`。
  两者使用原有镜像和数据卷，`.env` 与宝塔目标站点配置摘要未变化。
- 受检查的 PM2 进程身份与状态、同机站点 HTTP 响应均保持原基线。

恢复流程的 Web、图标、进程健康、管理员鉴权和执行锁检查通过。
公网行情与 K 线均恢复新鲜；SSE 行情事件间隔约 0.231 秒、系统心跳约 5.068 秒，
未认证私有 SSE 返回 401。TradingAgents 运行环境自检为 `ready`，
`execution_authorized=false`、`provider_connection_verified=false`，没有重新调用模型生成研究。

这是生产当前快照恢复的验收，不证明恢复历史私有账本后已与交易所完成补账，
也不包含旧应用镜像回滚、宿主机断电或真实 Demo 在途订单恢复。
恢复后保持急停、执行关闭、Worker 关闭、Dry Run 开启和实盘闸门关闭。

## 8. CI 时间预算核对

提交 `492d9f2` 的 push `35500235977` 与 PR `35500237408` 两轮 CI 均已完成并成功，
包含完整 API、Web、浏览器、TradingAgents 镜像及新增的容器进程崩溃恢复检查。

较早的文档提交 `2c59e2d` 的 PR CI `35499466311` 被 25 分钟任务上限取消。
日志显示 860 项测试运行了 1484.774 秒并输出 `OK`，随后任务被取消；
这条 CI 的整体结果仍是 `cancelled`，不能算通过。对应 push CI `35499464721` 成功。

API job 的上限已调整为 35 分钟，为较慢的共享 runner 留出安装与收尾时间。
测试命令、用例和断言没有减少；新的 CI 状态仍须按新提交单独核对。
