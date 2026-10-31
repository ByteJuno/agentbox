# agent-hub 与 agentbox 网页兼容性审查

日期：2026-10-31。对照本仓库的 Go 服务、`web/src`、`internal/web/static`、`internal/chat`、`internal/protocol`、`contracts/chat-errors-v1.schema.json`、`docs/api.md`、`docs/chat-protocol.md`、`docs/chat-recovery.md`，并核对 `ffa7ac6..e19df41` 的升级差异。改动全部位于 `hub/`；没有改上游 TS 或静态编译产物，没有提交或推送，没有接触生产服务、8765 端口或 `~/.agent-hub`。

## 主要结论与修复

最明显的问题集中在：新版网页要求每条线程独立的运行状态、广播和回执，而旧兼容层仍大量依赖空间的当前线程；历史窗口的新游标被当成普通最新页；回执恢复与编号清理会造成错误重试；新终端页首次加载的接口缺失。以下均已处理。

行号指本次审查时的仓库文件。表中的旧行为为修改前的 hub 行为；修复包含同一任务此前已开始的工作。

| 路由 / 消息 | 前端依据 | 上游依据 | 原不匹配与本次处理 |
| --- | --- | --- | --- |
| WS `/api/sessions/{id}/chat` 的所有回合事件 | `web/src/chat.ts:234`、`web/src/features/chat/running.ts:1` | `internal/server/chat.go:179`、`:243`、`:427` | 缺少 `thread`，并过滤掉非当前线程的事件；现在按执行线程广播，连接快照包含 `threads`、`elapsed_ms`、`max_parallel`，保留页面自己的阅读线程。 |
| `status` / `turn_cost` | `web/src/chat.ts:305`、`:332`、`web/src/features/chat/turns.ts:86` | `internal/chat/service.go:410`、`:423` | 最终状态可能先于费用收尾，费用字段形状不完整；现在先完成原有回执/用量观察，再发费用与最终状态，附墙钟 `duration_ms`；每次已入账 usage 也发 `running:true` 累计费用。 |
| Claude 完整事件 / streaming / 工具结果 | `web/src/chat.ts:266`、`web/src/features/chat/claude-tools.ts:1` | `internal/chat/service.go:31`、`internal/server/threads.go:525` | 核对并保留 Claude 原始事件封装、thinking/工具结果，增量只广播，完整事件写历史；恢复 rate-limit 事件，工具路径继续虚拟化。子 Agent 增量维持隐藏内部步骤的既定边界。 |
| 用户停止及停止超时 | `web/src/chat.ts:279`、`web/src/chat-render.ts:799` | `internal/chat/service.go:33`、`internal/server/chat_receipt_turn.go:1` | 接入前尚无 client 时停止会失败；旧超时只按 session ID 取消，可能误杀下一轮。现在标记用户中断、暂停队列，超时只取消原 Task；实时与历史中的中断 result 标记 `state:interrupted`，避免刷新后多出失败卡。 |
| HTTP `PUT …/chat/requests/{request}` | `web/src/features/chat/outbox.ts:148` | `internal/server/chat_requests.go:82`、`internal/store/chat_requests.go:191` | 曾要求发送到空间当前线程，且并发准入未串行；现在校验目标属于该空间，按线程准入，在锁内持久化后安排一次执行，冻结模型/强度/附件及输入指纹；排队时就分配稳定 `turn_id`。 |
| queue / steer / withdraw | `web/src/features/chat/outbox.ts:187` | `internal/server/chat_queue.go:110`、`:204` | 旧队列绑定 native Session 的当前选项，无法完整表达空间并发上限；现在网页队列保存原输入与原编号，按 FIFO 等空位，重读上限，uncertain 阻挡本线程；steer 复用原 transport，无法插入则排队；撤回保留正文供恢复，重放不再执行。 |
| GET 回执及 `pending_only=1` | `web/src/features/chat/outbox.ts:99`、`:113` | `internal/server/chat_requests.go:186`、`:216` | 原来只暴露单个 pending，可能把另一线程状态套给本页；现在返回所有 `pendings`，优先选择查询线程的 `pending`，轮询只取排队正文与未结束回执，孤儿执行转 uncertain。 |
| 重启 / review / 编号保留 | `web/src/features/chat/outbox.ts:160`、`:187` | `internal/store/chat_requests.go:370`、`docs/chat-protocol.md` | 重启把未结束回执误记 failed；超过一定数量直接删旧编号会允许迟到重放。现在重启记 uncertain，必须核对后 review，不自动重跑；正文到期可清，编号永久保留，写入使用原子替换和 fsync。 |
| CLI 非零退出与未知流结束 | `web/src/features/chat/outbox.ts:113` | `internal/server/chat_receipt_turn.go:1`、`docs/chat-protocol.md` | 原来未区分确定失败与可能仍有副作用的异常。现在 SDK `ProcessError` 有明确非零退出码时记 failed/turn_failed；无法确认退出或无 result 时记 uncertain；服务停止不会冒充用户停止。 |
| GET `…/history?before/after/until/from` | `web/src/features/chat/older.ts:63`、`web/src/features/chat/window.ts:181`、`web/src/chat.ts:1164` | `internal/server/threads.go:510`、`:525` | `after/until/from` 以前未正确实现，非当前线程分页还会改变归属。现在返回 `start/end/has_more/has_newer`、正确的 `thread_id` 或 `active_thread`，校验游标、限制页大小、补 `turn_context`；读取其他线程不改 active 指针。 |
| 历史追加顺序 / outline | `web/src/features/chat/window.ts:133`、`web/src/chat.ts:1142` | `internal/server/thread_outline.go:13`、`internal/server/threads.go:510` | 原先把随 CLI 压缩变化的转录和 hub 事件重新排序，游标可能漂移。现在 hub 事件为主序列；接手前的转录冻结到事件旁的 `.history.json` 前缀，随后只追加；outline 的 `line` 使用同一序列。 |
| `…/chat/threads`、activate/rename/delete | `web/src/chat-threads.ts:118`、`:241`、`:266`、`:300` | `internal/server/threads.go:672`、`:692`、`:727`、`:805` | 增加空间归属验证，防止跨空间线程操作；允许其他线程运行时新建/切换；空线程复用且不混入已发送列表；删除保留回执围栏，删除非当前的空线程不会切走当前线程。标题继续使用首条输入与手动改名；顺序及本机未读按更新时间工作。 |
| `POST …/chat/questions/{question}` / tasks | `web/src/features/chat/questions.ts:264`、`web/src/chat.ts:121` | `internal/server/chat_questions.go:1`、`internal/server/chat_tasks.go:52` | 提问/任务查询原先缺少线程归属约束；现在检查问题属于该空间，验证答案类型，已结束问题返回 question_closed；审批继续包装为两选项提问，不改变 native API。 |
| `/api/settings`、`/api/me` / `chat_limit` | `web/src/features/settings/controller.ts:849`、`:870`、`web/src/chat.ts:234` | `internal/server/settings.go:36`、`:88`、`internal/server/chat.go:461` | 原来保存多数设置会假成功，新增 max_parallel_chats 缺失。现在支持 1–10 上限并广播/调度；历史超时、界面时区、提示语和新空间默认模型持久化；未实现设置明确拒绝。超时的上游保存组会携带固定权限/上传/监听原值，因此接受原值回传，拒绝修改。 |
| `/api/sessions` / creation 回执 | `web/src/features/workspaces/create.ts:171`、`web/src/features/workspaces/creation-flow.ts:35` | `internal/server/workspace_creations.go:135`、`internal/server/sessions.go:1` | 直接创建以前拒绝，创建指纹只比名字，旧记录被删后可能再创建。现在兼容直接创建、完整输入比对和永久围栏，未知编号也可先 abandon；删除/放弃不允许迟到 PUT 重新建空间，默认模型只用于新空间。 |
| `/api/sessions/{id}/stop`、`…/term` | `web/src/sessions.ts:205`、`web/src/term.ts:539` | `internal/server/termws.go:151`、`internal/server/client_terminals.go:73` | 空间停止时曾有队列再启动窗口；现在先禁止调度再停所有线程及 tmux。打开主终端会启动空间；无效 resize 不再断连接，发送协程在断开时正确回收。新增独立终端入口隐藏，列表返回空数组。 |
| `/api/sessions/{id}/attachments/validate` / preview | `web/src/features/chat/drafts.ts:43`、`web/src/preview.ts:307` | `internal/server/images.go:37`、`internal/server/preview.go:27` | 附件只用 isfile 会跟随链接，预览链接也能跳出获准目录。现在附件逐级拒绝链接，文件路径检查真实范围，预览逐次检查其单目录边界；保留 30 分钟只读 grant 和不含 allow-same-origin 的 sandbox。 |
| `/api/themes`、validate、PUT user | `web/src/themes.ts:59`、`:311`、`web/src/theme-editor.ts:146` | `internal/server/themes.go:41`、`:99`、`internal/theme/manifest.go:1` | token 列表为空、validate 形状不对且基本未校验、保存返回整个目录而非 ThemeView。新增白名单/CSS 取值校验，返回 normalized manifest 与单条 ThemeView，修复编辑器。全站主题仍关闭。 |
| `/api/pricing`、delegation stats、Git operations | `web/src/pricing.ts:232`、`web/src/delegation-tags.ts:37`、`web/src/git-operations.ts:151` | `internal/server/pricing.go:155`、`internal/server/delegation_api.go:130`、`internal/server/git_operations.go:323` | pricing、stats 的根形状不匹配；Git 记录接口缺失。现在分别返回 active/candidate/changes/warnings、`{stats:[]}`、`{rows,active,next_before}`；本地 commit/discard 的现有进程内记录可显示。 |
| `/api/system` / monitor | `web/src/features/settings/operations.ts:24`、`:88` | `internal/server/settings.go:154`、`internal/server/monitor.go:1` | running 曾按对话数统计成空间数，监控还把整台主机用量当成容器用量。现在空间数与其 start/stop 视图一致；主机、Python 进程保留实测，容器列表为空，不再冒充容器资源。 |
| 错误封装、logout、鉴权 | `web/src/api.ts:15`、`web/src/problems.ts:1`、`web/src/login.ts:151` | `internal/server/problems.go:35`、`contracts/chat-errors-v1.schema.json` | 兼容错误补 operation_id/hint/action/retryable 及响应头，gone/attachments 使用约定状态码；logout 清 cookie。只凭 cookie 的写请求仍校验本站来源，额外拒绝跨站 WS；旧 WS 发送也遵守 pending 和并发准入。 |

## 路由盘点及未启用能力的处理

用 `rg` 搜索 `web/src` 的 `api(`、泛型 api、`fetch(`、`wsURL(`、`/api/`，并追踪 prefix/endpoint 等动态拼接及上传 XHR。下表按路由族列出调用；`S` 表示 `/api/sessions/{id}`，`R` 表示 `S/chat/requests/{request}`，`C` 表示 `/api/session-creations/{request}`。HEAD 使用 aiohttp 的 GET 配套行为。

“保留”表示核对后继续使用原有实现，并有新增工作流测试覆盖；“关闭”表示入口保持隐藏/不可用，读取提供前端期望的关闭形状，写入明确返回 JSON 错误，不伪造成功。无相关产品入口的动态子路径同样拒绝，不执行操作。

| 前端路由族（HTTP，注明 WS 的除外） | 处理 / 实现位置 | 调用依据 |
| --- | --- | --- |
| POST `/api/login`、`/api/logout`；GET `/api/me`、`/api/ping`、`/api/icons` | 实现；icons 读取保持默认图标，写覆盖关闭 | `login.ts:70`、`:124`、`:151`，`icons.ts:96` |
| GET/POST `/api/sessions`；GET/PATCH/DELETE S；POST S/start、S/stop；PUT S/account | 实现；账号仅接受现有 host 账号，不切换身份 | `data.ts:20`，`sessions.ts:190`、`:205`、`:271`、`:330`、`:371` |
| GET `/api/session-creations`、GET/PUT/POST C；POST C/upload、C/git | 空项目创建/查询/完成/放弃实现；项目导入关闭 | `features/workspaces/creation-flow.ts:35`、`:97`、`:126`，`features/workspaces/create.ts:171` |
| GET `/api/onboarding`、`/api/accounts` | 实现单人快照与只读 host 账号 | `onboarding.ts:100`，`sessions.ts:299` |
| POST `/api/accounts`；PATCH/DELETE `/api/accounts/{id}`；POST `…/models/discover`、`…/oauth/start`、`…/oauth/finish`、`…/apikey/test`；POST/DELETE `…/apikey` | 关闭；添加、认证、编辑及可用模型编辑入口隐藏 | `features/settings/controller.ts:395`、`:994`、`:1032`、`:1054`、`:1070`，`account-models.ts:229` |
| GET S/account/usage | 保留只读订阅查询及短缓存，不主动刷新 native CLI 的令牌 | `acct-usage.ts:147` |
| GET S/models、S/commands、S/chat/tasks、S/outline | 实现/核对；模型、命令、任务保留 host CLI 对应方式 | `chat.ts:121`、`:127`、`:749`、`:1142` |
| GET/POST S/chat/threads；POST S/chat/reset、`…/threads/{tid}/activate`；PATCH/DELETE `…/threads/{tid}` | 实现 | `chat-threads.ts:118`、`:241`、`:266`、`:279`、`:300` |
| GET S/history；WS S/chat；PUT/GET/POST R；GET S/chat/requests；POST S/chat/questions/{question} | 实现；详见上表 | `chat.ts:1095`，`features/chat/outbox.ts:99`、`:113`、`:148`、`:187`，`features/chat/questions.ts:264` |
| GET/DELETE S/files；POST S/files/move、mkdir、rename；GET/PUT S/file | 保留工作流，增加真实路径边界检查 | `files.ts:90`、`:495`、`:556`、`:628`、`:635`，`preview.ts:62` |
| POST S/upload、S/images、S/attachments/validate；GET S/archive、S/preview、S/files/search | 保留/修复；multipart 文件按既有限额流式接收，独立预览 grant 继续可用 | `api.ts:106`、`:123`，`features/chat/drafts.ts:43`，`preview.ts:307`，`chat.ts:142` |
| GET S/git/status、diff、file；POST S/git/commit、discard；GET/PUT `/api/me/git` | 保留本地 Git；不推送、不改全局身份 | `changes.ts:88`、`:285`、`:384`、`:428`，`git-profile.ts:44` |
| GET `/api/git/operations` | 新增 rows/active/next_before；仅现有本地进程内记录 | `git-operations.ts:151` |
| GET `/api/git/connections`、`/api/git/oauth/apps`、S/git/bindings、S/git/terminal-grants | 空数组 | `git-connections.ts:60`、`:202`，`git-oauth.ts:24`，`git-terminal.ts:24` |
| GET `/api/me/git/default`、S/git/default | `{connection_id:""}` | `git-connections.ts:60`、`:202` |
| Git connections/{id}（含 shares）、oauth/start、oauth/apps 写操作；PUT 默认/bindings；POST terminal-grants、DELETE grants/{id} | 关闭；通配子路径统一拒绝 | `git-connections.ts:77`、`:164`、`:242`，`git-shares.ts:24`，`git-oauth.ts:34`、`:86` |
| S/git 的 branches、remotes、reviews（含 preview/创建/切换等子操作）、fetch、pull、push、push-preview、clone；Git operations/{request} 查询/取消 | 关闭；本地 status 中的远程元数据只读保留 | `git-branches.ts:25`，`git-reviews.ts:49`，`git-operations.ts:94` |
| WS S/term | 保留 tmux 主终端，修复 start 与 resize/断开收尾 | `term.ts:1` |
| GET S/client-terminals、S/client-projects | **原先完全缺失**；现在返回 `[]`，隐藏新建独立终端按钮 | `term.ts:539`、`:540` |
| POST S/client-projects、S/client-terminals；DELETE terminal/{id}；WS terminal/{id}/stream | 明确关闭 | `term.ts:564`、`:582`、`:608` |
| GET/POST S/skills；GET/DELETE S/skills/{name}；GET `…/file`；POST `…/copy` | 保留两种范围、本地上传、树与复制/删除；真实 HTTP 工作流已覆盖 | `skills.ts:91`、`:314`、`:368`、`:562`、`:590`、`:660` |
| GET `/api/marketplace`；POST S/skills/market | **读取原先完全缺失**；现在 plugins/categories 空数组，安装关闭，市场入口继续隐藏 | `skills.ts:708`、`:785` |
| GET `/api/mcp`、S/mcp；相关增删改/check 子路由 | **读取原先完全缺失**；现在 revision/items/external/project_names 空视图；写操作关闭 | `mcp.ts:71`、`:80`、`:177`、`:183` |
| GET/PUT `/api/settings`；GET `/api/system`、`/api/monitor`、`/api/storage/quotas` | 支持的设置真实保存，其余明确拒绝；监控只报告实际能力 | `features/settings/controller.ts:637`、`:691`，`features/settings/operations.ts:24`、`:88` |
| GET `/api/usage/events`；WS `/api/usage/live` | 保留原用量账本、筛选、分页与实时提示，时区与保存设置同步 | `usage.ts:833`、`:907`，`usage-live.ts:47` |
| GET `/api/pricing`；PUT 同路由；POST check/apply/restore | 修复读取形状；价目编辑、目录自动更新/恢复关闭 | `pricing.ts:232`、`:292`、`:382`、`:465`、`:477` |
| GET `/api/themes`；POST validate；PUT/DELETE user/{id}；PUT/DELETE site/{id} | 个人主题实现；全站写入关闭并声明 can_manage_site=false | `themes.ts:59`、`:311`、`:335`，`theme-editor.ts:146` |
| GET `/api/announcement`、`/api/announcement/activity`；PUT announcement | 无通告/空活动视图；写入关闭，管理入口隐藏 | `announcement.ts:91`，`announcement-admin.ts` |
| POST `/api/diagnostics`、S/diagnostics；WS 两者的 /ws | **原先完全缺失**；现在只检查本实例读写与 WS 连通性，Docker/镜像/模型明确 not_checked | `diagnostics.ts:52`、`:130` |
| GET `/api/users`；POST users；DELETE user/{name}；POST password/credits；GET/PUT quota；PUT storage；POST `/api/me/password` | 只读单用户列表；团队、额度管理与密码修改关闭 | `features/settings/controller.ts:692`、`:760`、`:786`、`:1110`、`:1128`，`quota.ts:145`、`:249`、`:275` |
| GET `/api/proxies`；test/import/export、PATCH/DELETE proxies/{id}、POST proxies | 空只读列表；写入/检测/导出关闭 | `proxies.ts:32`、`:183`、`:290`、`:323`、`:351` |
| GET `/api/updates`、upgrade、components；POST check、upgrade；GET image-updates、POST actions | 升级关闭形状；版本组件只读，镜像操作关闭 | `updates.ts:145`、`:258`、`:309`，`features/settings/update-components.ts:31`，`features/settings/image-updates.ts:97` |
| GET `/api/tunnel/status`、clients、clients/{name}；POST pair/probe | enabled/connected=false、空客户端下载列表；其余关闭 | `tunnel.ts:26`、`:75`、`:144`、`:189` |
| GET S/keepalive；PUT 同路由；POST /stop | available=false/reason=disabled；写入关闭 | `features/workspaces/keepalive.ts:23`、`:173`、`:199` |
| S/browser（GET/POST/DELETE）、clipboard（GET/POST）、WS desktop | available=false，启动/剪贴板/桌面关闭，入口隐藏 | `remote-browser.ts:60`、`:84`、`:97`、`:149`、`:161`、`:168` |
| GET `/api/extensions`；GET extension/{id}/events、logs；PUT extension/{id} | 空 extensions；子操作关闭 | `extensions.ts:111`、`:134`、`:135`、`:463` |
| GET S/delegation/options、`/api/delegation/stats`；GET S/delegations/{id}/log | available=false、`{stats:[]}`，日志关闭；`/me` 的 delegation_protocol=0 | `features/delegation/composer.ts:165`，`delegation-tags.ts:37`，`features/delegation/card.ts:219` |

新增 `hub_optional.py` 集中承接上述缺失/关闭路由，避免依赖 HTML 404 或错误根形状。不是所有上游可选功能都有同一个“feature off”协议：列表保留对应数组/对象，已有 available/enabled/supported 的使用这些字段；不可执行的写操作返回错误，不返回假的成功记录。

## 保留的产品选择与明确未做

- 保持主机直跑、单用户、host 登录身份；网页新线程仍默认 bypassPermissions。`/workspace`、`/shared` 的显示和附件真实路径转换保留。
- 没有实现 Docker、账号池/代理、多用户、隧道、远程浏览器、扩展、MCP 管理、技能市场、Git 远程/分支/PR/clone、项目上传或 Git 导入；没有为这些功能启动后台任务。
- `/hub`、Unix socket、Eta、agentctl 的原有命令与返回结构保留。网页的并发限制不强加给 native API；native 删除空间现在也会清网页回执正文并保留编号围栏。
- 不新增付费起标题请求或下一步预测：标题继续使用首条消息/手动标题，`prompt_suggestions=0`；不为补齐 UI 额外调用模型。
- 用量继续使用 hub 原来的逐请求账本和 API 估价，不改成上游 SQLite/余额账本，不追扫所有终端或外部 CLI 会话。Git 操作记录当前只在本进程内保存，重启后不恢复。
- 未实现容器级资源/配额或 Windows PTY；监控 host/process 与可见空容器视图已核对。未改线上部署、系统单元或真实账号配置。
- 旧版 CLI 工具结果已有 40,000 字符截断保留；没有把长工具输出或子 Agent 内部每个步骤无限展开。

## 验证与证据边界

本次已执行：

1. `AGENT_HUB_TG=0 AGENT_HUB_MAX_TURNS=30 /home/ubuntu/ai-work/agent-hub/.venv/bin/python -m unittest discover -s hub/tests -q`：**54 个测试通过**。新增兼容测试覆盖真实 aiohttp HTTP/WS、两线程运行快照与事件分流、并发队列、稳定 turn_id、steer/withdraw、重放/冲突/重启 uncertain/review、停止、真实 SDK 执行器异常分类、历史双向窗口及接手前缀压缩、问题回答、创建/删除围栏、附件/预览/鉴权、主题、设置以及本地文件/技能/Git 工作流。终端帧测试使用真实 PTY + cat，不启动真实 tmux 或 shell。
2. `python3 -m py_compile hub/*.py hub/tests/*.py`、`node --check hub/web/hub/hub.js`、`git diff --check`：通过。
3. `hub/tests/browser_compat.py`：真实 Chromium 加载原版上游资源，在随机回环端口的独立 aiohttp 实例中通过登录、发送一次合成回合、运行中刷新恢复、中断/发送按钮状态、回答和工具结果、文件/Git/技能切页、并发上限与历史超时的真实保存、监控/关于/用量页面、390px 窄屏。无 JS pageerror、无失败 HTTP 响应；刷新没有重复执行。测试代码可重跑，需要 Python Playwright 和 Chromium，可用 `AGENT_HUB_BROWSER_EXECUTABLE` 指定已有浏览器。
4. 真正从本 checkout 启动一次 `hub/hubd.py`，状态在 `/tmp/hubdev-codex-*`，关闭 Telegram，只监听 `127.0.0.1:18766`；首页、me、settings、themes、pricing、sessions、monitor 七个入口均为 200，JSON 正常。随后仅终止这个子进程并等待退出。令牌未输出；未发送任何真实模型请求。

没有改 `web/src`，不需要重新生成上游 JS；没有改 Go 代码，本次未跑需要 Docker/root 的 Go 服务端组。测试中的费用修复锁回归会打印“先停止服务”的既有提示，那是临时文件锁的合成测试，**没有实际停止任何服务**。

仍需要真实 Claude + 浏览器人工验收的部分：

- SDK/CLI 实际版本的完整流：连续文字与 thinking、长 Bash 心跳、Read/Edit diff、后台任务、限流、权限/AskUserQuestion、多选/跳过/超时、中断后的实际进程退出；合成事件不能证明真实版本兼容。
- 两个真实线程并行、多页面各看一条，切换/改名/未读、steer 在 CLI 真正回显时生效与来不及生效时 dropped，跨页面停止；核对只发生一次工具副作用和一次用量记录。
- 真正断网及自有开发实例重启时的 outbox 查询、uncertain 核对和后续队列；不能用生产重启验证。本次测试没有为了验收停止线上服务。
- 长历史在真实 CLI 自动压缩后的阅读/导航，已接手会话的旧记录与新增回合连接；与真实大文件工具结果的展开体验。
- 真正 tmux 的重连/顶号/空间停止、服务重启后留存、粘贴/上传和触屏行为；PTY+cat 只证明帧协议。
- 订阅额度查询与真实计费估值、长上下文/缓存的实际数据，真实终端运行产生但未经过 hub 的用量差异。浏览器测试未访问生产凭证，也未消费真实模型额度。
