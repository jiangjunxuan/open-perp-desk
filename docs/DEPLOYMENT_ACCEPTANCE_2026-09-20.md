# OpenPerpDesk 生产重启与恢复验收（2026-09-20）

本文分阶段记录阿里云上海目标服务器的重启、进程故障、快照恢复和镜像回滚验收。
第 1 至 5 节记录部署脚本 `4c0e2a4` 的真实重启行为：该阶段只更新
`infra/deploy.py` 和 `infra/okx-demo-lifecycle-smoke.py`，没有构建或替换应用镜像。
后续镜像切换见第 10 节。所有阶段均未配置私有账户，也没有执行交易。
第 11 节开始启用只接收模式的 TradingView；前文“未启用”是当时的历史状态。
第 12 节记录宿主机启动检查及异机备份，不代表已经完成断电恢复。

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

以上同镜像重启记录不包含下列事项；后续进程故障、当前快照恢复和镜像回滚分别见第 6、7、10 节：

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

## 9. 旧版与当前 API 镜像的隔离兼容性

2026-09-20 09:18:20 至 09:18:37 UTC，在同一目标服务器完成隔离 API 镜像兼容性检查。
先生成新的在线快照，再将其复制到独立临时 Docker volume；生产容器没有停止或切换。

- 源快照：`openperpdesk-20260920T091819Z-d79640b5.sqlite3`。
- SHA-256：`05cc182c0dd9ed6c660861e51ae2a3974ee1f24b316a47ed6cf3c8271f61fe72`。
- 旧版 API：`openperpdesk-api:release-d3e74eb-tradingagents-v3`，
  digest 为 `sha256:b6fd9109cc0f3b80d149e69334ef4f2cb5598161bdce308124aa1a570b0a2619`。
- 当前 API：`openperpdesk-api:release-71fccf3-tradingagents-v4`，
  digest 为 `sha256:c0d140b3446a1cab78f56d48342337ce6f0c5b832677a5ceed8c3437e8e7ad62`。
- 两阶段均按 digest 启动，依次挂载同一临时数据卷，使用 `network=none` 且不发布端口。
  临时容器没有交易、通知、模型或代理密钥；执行关闭，Worker 关闭，Dry Run 开启。
- 两阶段均通过 API 进程健康、管理员鉴权、历史报告读取和持久安全锁检查。
  未认证账户接口与私有 SSE 均返回 401。
- 26 张表（包含审计表和 `sqlite_sequence`）全部记录摘要保持一致，保留 119 条审计和
  56 份报告。两个阶段导出的快照 SHA-256 也均与源快照一致。
- 前后生产容器身份、镜像、数据卷、账本、`.env`、宝塔目标站点配置，以及所检查的
  PM2 进程和同机网站响应保持不变。
- 临时容器与临时卷已移除，源快照及两阶段验证快照保留。
  证据为 `/opt/openperpdesk/outputs/image-compatibility-20260920.json`，权限 `600`。

本次只证明这两个 API 镜像对该无私有交易快照的隔离启动与读取兼容性。
没有切换生产镜像，没有验证旧版 Web、外网行情、模型、真实账户或在途订单；
该隔离检查的 `production_image_switch_verified=false`，不证明生产回滚切换或宿主机断电恢复。
后续生产镜像切换的独立证据见第 10 节。

## 10. 生产旧镜像切换与当前镜像恢复

2026-09-20 09:52:40 至 09:53:52 UTC，在目标 `openperpdesk` Compose 项目中完成
API 与 Web 的旧镜像切换验收，再自动返回原来的当前镜像。这是短时受控维护，
不是无中断升级；没有重启宿主机、Docker daemon、全局 Nginx 或同机其他项目。

执行前确认无私有凭据、无订单或持仓、无待处理账单任务、研究空闲、Worker 关闭、
执行关闭且持久急停为真。持有项目部署锁，确认只有目标 API 能写入状态卷，
先备份配置和在线数据库，再停止 API 并生成停写边界快照。

- 配置归档：`/opt/openperpdesk/releases/source-backups/image-rollback-20260920-f1c376fc05/`。
- 停写快照：`openperpdesk-offline-20260920T095242Z-9c99fb8c.sqlite3`。
- SHA-256：`05cc182c0dd9ed6c660861e51ae2a3974ee1f24b316a47ed6cf3c8271f61fe72`。
- 旧版 API 使用第 9 节所列 `d3e74eb` digest。
- 旧版 Web 为 `openperpdesk-web:release-d3e74eb`，
  digest 为 `sha256:79b108f15636753f3c891103b4db86e225206a12639442228523d6a03c29a6e0`。
- 旧版期间的 API / Web 容器分别为 `9a9cce7648c0` / `b55dd1ebff47`。
- 返回后的 API / Web 容器分别为 `5091c3c4c781` / `4114f0a54dd4`，镜像恢复为
  `release-71fccf3-tradingagents-v4` / `release-c37f5d9` 及原 digest。

旧版与返回当前版均验证：

- 容器健康、API readiness、管理员鉴权和持久安全锁正常。
- 公网首页、`app.js`、`styles.css` 内容摘要分别与当阶段 Web 容器文件一致，
  不只是检查 HTTP 200。
- 公网系统 SSE 心跳约 5.05 秒，行情事件约 0.246 秒，未认证私有 SSE 返回 401。
- 26 张表的全部记录及自增编号摘要与停写快照一致，保留 56 份研究报告、119 条审计、
  1 套策略和 3 条标记，没有恢复旧数据库或倒回用户历史。
- 原数据卷、`.env` 文件、宝塔目标站点配置、受检查 PM2 进程和同机网站响应保持原基线。

旧版阶段只通过临时进程环境覆盖镜像和管理员令牌，已验证原管理员令牌暂时被拒绝，
防止外部管理写入干扰演练；没有编辑 `.env`。切回后验证原令牌恢复有效、临时令牌失效。
失败分支的本地 5 项控制流测试覆盖旧版失败后切回、恢复失败只停止 API，以及拒绝无关配置
变化；本次生产流程没有触发失败分支，不将本地测试表述为生产故障注入。

阶段快照分别为 `openperpdesk-20260920T095310Z-deefca22.sqlite3` 与
`openperpdesk-20260920T095345Z-b6fd3ddf.sqlite3`。
证据为 `/opt/openperpdesk/outputs/production-image-rollback-20260920.json`，权限 `600`；
执行进程已结束，`accepted=true`、`production_old_images_verified=true`、
`current_images_restored=true`。随后独立读取线上容器、配置和账本，并从本机复测公网 SSE，
结果与完成记录一致。

该项完成的是无私有交易状态下的生产旧/新镜像切换，不包含真实 Demo 在途交易恢复、
模型研究重跑、所有旧版页面交互或宿主机断电。执行、Worker 和实盘闸门继续关闭，急停保留。

## 11. TradingView 只接收配置与公网协议验收

2026-09-20 10:50:34 至 10:50:53 UTC，已启用目标服务器的 TradingView 接收器。
只有下列三项开关和独立 Webhook 密钥发生变更；完整 Compose 配置差异经过核对，
没有改变 OKX、模型、管理员、代理或其他服务配置。

```dotenv
TRADINGVIEW_ENABLED=true
TRADINGVIEW_EXECUTION_ENABLED=false
TRADINGVIEW_DRY_RUN=true
```

先归档 `.env` 并生成一致性快照，再原子替换配置和重建 API。API 容器由
`5091c3c4c781` 变为 `03ff82463fe4`，Web 容器仍为 `4114f0a54dd4`；
镜像 digest、数据卷和目标宝塔站点配置不变。26 张表的已有记录全部保留，
配置完成时的快照为 `openperpdesk-20260920T105052Z-6e920e60.sqlite3`，
SHA-256 为 `05cc182c0dd9ed6c660861e51ae2a3974ee1f24b316a47ed6cf3c8271f61fe72`。
执行、自动交易和实盘闸门仍关闭，持久急停有效。

11:03:32 UTC 完成从本机到公网 HTTPS 的受控协议检查。测试报文固定
`action=hold`、`dry_run=true`，编号 `manual-https-check-20260920-88efa0cf1833`。
这条请求由本机脚本发送，**不是 TradingView 平台发出的真实 Alert**。

| 检查 | 实测结果 |
| --- | --- |
| 持久化接收回执 | HTTP 202，约 0.059 秒 |
| 已认证私有 SSE 告警事件 | 约 0.263 秒收到 `observed` |
| 完全相同的重复告警 | 幂等回执，收件箱仅一条记录 |
| 相同 ID、不同内容 | HTTP 409 |
| 错误 Webhook 密钥 | HTTP 401 |
| 过期告警 | HTTP 422 |
| 未认证读取告警收件箱 | HTTP 401 |
| 密钥保密 | 私有收件箱未包含 Webhook 密钥 |
| 交易 | 未调用下单，订单数保持 0 |

真实 TradingView 接收测试不依赖 OKX 私有凭据，可以先用 `hold` 验证；
之后的 OKX Demo 下单、成交和保护才需要单独的账户验收。
本次新开的 TradingView 页面在告警入口显示注册/登录提示，尚未创建平台 Alert。
必须观察平台告警日志与服务器同一 ID 的接收记录后，才能将
`real_tradingview_delivery_verified` 从 `false` 改为 `true`。

证据与受保护模板：

- 服务器配置验收：`/opt/openperpdesk/outputs/tradingview-readonly-setup-20260920.json`。
- 本机协议验收：`work/deployment/tradingview-https-receiver-20260920.json`。
- 平台消息模板：服务器 `outputs/tradingview-readonly-template-20260920.json`，
  本机 `work/deployment/` 也有受保护副本；含独立密钥，权限 `600`，不进入 Git。

以上证明公网接收、持久化、去重、拒绝路径和告警事件推送，不证明 TradingView
平台实际投递、浏览器本次视觉验收、OKX 交易或微信通知送达。

## 12. 断电前启动检查与异机备份

2026-09-20 11:11:01 UTC 完成目标宿主机只读检查，没有执行宿主机重启或断电：

- Docker 和 `pm2-root` 为 `active`、`enabled`；Docker 配置在网络在线目标之后启动。
- Nginx 与宝塔为活动的 SysV 生成服务；已核对运行级别 2 至 5 的启动链接。
- API、Web 均为 `healthy`，重启策略为 `unless-stopped`。
- API `/data` 为原有可写命名卷；文件系统为 `/dev/vda3`、ext4，
  当时可用空间为 13,725,691,904 字节。
- PM2 保存清单与当前进程的名称、状态及所检查的启动参数摘要一致；
  `xuanzhangge-cn` 仍在线、PID 794007、重启数 0，`konggang-dashboard` 仍停止。
  没有执行 `pm2 save`、`pm2 resurrect` 或重启无关应用。
- 同机 HTTP 基线仍为 Wukong 200、touch 403；后者不是健康验收通过。

新一致性快照包含上节的一条 `observed` 告警，另保留 56 份报告、1 套策略、
3 条图表标记和 120 条审计；订单、持仓、成交均为零：

- 快照：`openperpdesk-20260920T111059Z-cc9b8435.sqlite3`。
- SHA-256：`6bd259bd8122b4a8b7fc21ff48777c504d8b23f3f0c56fe340b6b55e09e144d5`。
- 服务器受保护归档：`/opt/openperpdesk/backups/powerloss-preparation-20260920/`。
- 本机异机副本：`work/deployment/offhost-20260920/powerloss-preparation-20260920/`。

归档包括数据库、原快照清单、`.env`、Compose 主配置与覆盖配置、部署脚本和目标站点
Nginx 配置，共 8 个文件，另附总清单。异机副本逐文件 SHA-256、文件大小和权限核对通过，
SQLite `integrity_check=ok`，26 张表记录数与源快照一致。目录权限 `700`、
文件权限 `600`，全部排除出 Git。此归档不是完整宿主机备份，不含其他项目或完整镜像。

宿主机检查证据为 `outputs/powerloss-preparation-20260920.json`，
异机核对证据为本机 `work/deployment/offhost-backup-verification-20260920.json`。
前者生成时尚未复制离机，所以其中 `offhost_copy_verified=false`；
复制完成后的独立核对记录为 `true`，不改写前一阶段原始证据。

维护时间、影响范围和云控制台开机恢复入口仍需确认。
执行步骤与停止条件见 [`MAINTENANCE_WINDOW.md`](MAINTENANCE_WINDOW.md)。
`host_reboot_performed=false`、`host_power_loss_verified=false`，
不能以启动配置、备份或先前的容器故障演练替代整机断电验收。

## 13. 自动 Worker 安全补丁与维护基线更新

2026-09-20 13:44:33 至 13:44:58 UTC，发布 `e310145` 的自动 Worker 安全补丁。
`AUTO_TRADING_DRY_RUN` 只有明确为 `false` 才能关闭，空白或拼写错误保持预览；
每个非预览周期还独立检查 Demo 模式，即使实盘客户端已经人工解锁，
这个 Demo 自动 Worker 也会在账户同步和策略执行之前拒绝运行。

- 新 API 镜像：`openperpdesk-api:release-e310145-worker-safety`。
- Digest：`sha256:dab4cbaea81d17c236ca7892c4bd56e03cbafb35b4a9021fa7bb8f0210a43e73`。
- 保留原 API 镜像全部层和运行配置，只增加 `automation_worker.py` 文件层。
  Docker legacy builder 的父镜像字段单独核对，其余配置要求完全相同；
  TradingAgents 运行环境与依赖没有重建。
- 新 API 容器为 `aa3022c023c3`；Web 容器仍为 `51e751ce5bb5`，
  镜像仍为 `release-43f98c8`，启动时间及重启计数未变化。
- `.env` 只修改 API 镜像选择项，完整 Compose 配置差异核对通过。
  数据卷、重启策略、管理员/模型/代理/Webhook 密钥和目标站点 Nginx 配置保持不变。
- 发布前确认研究空闲、没有 OKX 私有凭据、订单/成交/持仓为零。
  发布后执行关闭、Worker 关闭、Dry Run 开启、持久急停有效、实盘闸门关闭；
  TradingView 继续只接收、不下单。
- 发布前后 26 张表全部记录摘要一致，仍为 56 份报告、1 套策略、
  3 条图表标记、120 条审计和 1 条脚本测试告警。未恢复或覆盖数据库。
- 所检查的 PM2 进程身份、状态、重启数和同机网站响应保持原基线。
  Wukong 返回 200，touch 仍为 403，后者不记为健康通过。

定向回归 89 项通过，候选镜像在 `network=none`、只读根文件系统、
无生产密钥的隔离容器中通过 11 项 Worker 测试；发布脚本另有 7 项本地校验，
覆盖配置范围、父镜像、环境文件往返、并发配置保护及原子替换后的同步失败。
本次没有触发生产回滚，不把脚本单元测试写成生产故障注入。

发布后的独立检查确认运行文件 SHA-256 与提交一致，公网页面资源与 Web 镜像一致，
readiness 和管理员鉴权通过。公网系统 SSE 首事件约 0.086 秒、后续心跳约
5.098 秒；行情首事件约 0.066 秒、后续约 0.328 秒，未认证私有 SSE 返回 401。
这是 API 容器更新，不是无中断升级或整机电源恢复。

13:45:57 UTC 再次核对宿主机启动配置，并生成新维护备份：

- 快照：`openperpdesk-20260920T134555Z-6547b0d2.sqlite3`。
- SHA-256：`6bd259bd8122b4a8b7fc21ff48777c504d8b23f3f0c56fe340b6b55e09e144d5`。
- 服务器归档：`/opt/openperpdesk/backups/powerloss-preparation-e310145-20260920/`。
- 本机异机副本：`work/deployment/offhost-20260920/powerloss-preparation-e310145-20260920/`。
- 异机核对 8 个文件的摘要、大小和权限通过，SQLite 完整性和 26 张表记录数通过。
  该备份包含本次新镜像配置，不再以第 12 节旧配置作为当前维护基线。

发布证据为服务器 `outputs/worker-safety-e310145.json`，启动检查为
`outputs/powerloss-preparation-e310145-20260920.json`；本机保存两者副本，以及
`work/deployment/worker-safety-e310145-realtime.json` 和
`work/deployment/offhost-worker-safety-e310145-verification.json`。
宿主机 boot ID 前后一致，未执行整机重启或断电。

当前仍缺已登录且具备 Webhook 告警权限的 TradingView 账户、明确批准的整机维护时段，
以及经确认的阿里云控制台开机入口。收件箱仍只有第 11 节脚本测试告警，
`real_tradingview_delivery_verified=false`、`host_power_loss_verified=false`。
实际维护窗口开始前仍须再次核对状态和生成最新备份。
