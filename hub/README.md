# agent-hub

在 lab 上跑 Claude Code 会话的中枢：网页、Eta（原生界面，开发中）、agentctl 用的是同一批会话，
在哪个端都能接着聊。Agent 直接跑在 lab 主机上（不进容器），用的就是 lab 上已经登录的 Claude Code，
模型、设置、CLAUDE.md、技能和记忆都一样。

代码在 [ByteJuno/agentbox](https://github.com/ByteJuno/agentbox) 的 `hub` 分支（fork 自
[agentbox](https://github.com/devilcoolyue/agentbox)，Apache-2.0）。网页端直接用仓库里上游的原版前端
`internal/web/static`（跟上游合并就是升级前端），一个字没改；hub 按它的
接口格式提供后端。服务首页时只注入 `web/hub/` 下的一小段样式和脚本：藏起单人版用不上的入口（远程浏览器、
内网隧道、扩展、设置里的团队和容器分区），说明新建空间时名称怎么对应目录，以及旧 cookie 自动登录。

## 组成

| 文件 | 作用 |
| --- | --- |
| `hubd.py` | 守护进程：Claude Agent SDK 跑会话、排队、插话、审批、提问；HTTP + SSE + WebSocket；Unix socket 给 agentctl |
| `hub_abx.py` | agentbox 的对话接口：线程、历史（分页）、实时对话、消息回执（排队 / 插话 / 撤回 / 中断）、提问作答、任务面板 |
| `hub_spaces.py` | 空间列表与新建 / 改名 / 删除 / 停止、账号、使用记录、设置、资源监控、主题等外壳接口 |
| `hub_optional.py` | 未启用功能的空列表 / 关闭状态与明确拒绝；不调用模型的环境诊断 |
| `hub_theme.py` | 个人主题清单、令牌白名单与安全 CSS 取值校验 |
| `hub_files.py` | 文件页、聊天附件、HTML 预览直链、@ 文件搜索 |
| `hub_git.py` | 变更页：状态、diff、提交、丢弃 |
| `hub_term.py` + `tmux.conf` | 网页终端：每个空间一个 tmux 会话 |
| `hub_skills.py` | 技能页（本地上传、查看、复制、删除） |
| `hub_workspaces.py` | 工作空间登记（`/hub/workspaces`） |
| `agentctl` | 命令行，本机调试用 |
| `agent-hub.service` | systemd 用户服务 |

状态在 `~/.agent-hub/`：`sessions.json` 会话登记，`events/<会话>.jsonl` 事件流，`workspaces.json` 空间，
`receipts.json` / `abx-receipts.json` 消息回执，`usage.jsonl` 每次模型请求的用量，`shared/` 聊天附件，
`token` 是 HTTP 令牌（只有本用户能读）。

## 安装

```sh
python3 -m venv .venv && .venv/bin/pip install claude-agent-sdk aiohttp
cp agent-hub.service ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now agent-hub
sudo ln -sf "$PWD/agentctl" /usr/local/bin/agentctl
agentctl token        # 网页登录和 Eta 用的令牌，只在自己终端里看
```

网页：`http://100.120.32.105:8765/`，用户名随便填，密码填令牌。

## 跟 agentbox 的对应

- 工作空间 = lab 上的一个项目目录。新建时名称填 `~/ai-work` 下已有的文件夹名就直接打开，填 `~/路径`
  打开 home 下任意目录，不存在就在 `~/ai-work` 下新建（只建空目录）。删除空间只是不再登记，不动文件。
- 对话线程 = 一个 Claude Code 会话。终端里开的会话可以用 `agentctl adopt` 接手。
- 页面上的 `/workspace`、`/shared` 是显示用的：工具行里的路径换成相对路径、点了能直接打开文件；
  发给 Claude 的消息里附件路径会换回真实路径（`~/.agent-hub/shared/...`）。
- 终端：tmux 用 hub 自己的 socket（`tmux -L agent-hub`），服务进程放在单独的 systemd scope，hub 重启时
  终端里的程序不受影响。「停止」空间会结束它的终端会话和正在跑的对话。
- Git：变更页只看改动、提交、丢弃。提交身份用仓库自己的 git 配置，没配才用网页上保存的那份。
- 技能：「本空间」= 项目的 `.claude/skills`，「我的模板」= `~/.claude/skills`。
- 单人用：账号池、代理、多用户、内网隧道、镜像更新、远程浏览器都没有。
- 10-27 按机主的意思删掉、入口也藏起来的：MCP 页、技能的官方市场、新建空间时的上传项目 / 从 Git 导入、
  Git 远程（获取 / 拉取 / 推送）、分支、PR、克隆、仓库连接。这些让 Claude 用本机的 git / gh / claude 命令做，
  或者在终端里做。

## 安全

- HTTP 只听 `127.0.0.1` 和 lab 的 Tailscale 地址，公网访问不到；家里内网经路由器的 Tailscale 子网路由访问。
- 所有接口都要令牌：agentbox 前端和 Eta 用 `Authorization: Bearer`，WebSocket 和下载链接带 `?token=`（只认 GET）。
  只靠 cookie 的写请求必须来自本站页面。HTML 预览走单独的只读预览直链（只限那一个目录、30 分钟过期、
  沙箱里拿不到令牌）。
- 权限模式默认 bypassPermissions（全部自动通过）；网页上能用终端，等于这台机器的 shell。

## hub 自己的接口（Eta、脚本用）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/hub/info` | hub 名字、平台、home、可选权限模式和模型 |
| GET | `/hub/sessions?all=1` | 会话列表；`all=1` 时再列 lab 上其它 Claude Code 会话（`other`） |
| POST | `/hub/sessions` | 新会话 `{cwd, name, mode, prompt, request_id, model, effort}` |
| GET | `/hub/sessions/{id}` | 会话详情，含待审批 `pending`、待回答 `questions`、排队 `queue` |
| POST | `/hub/sessions/{id}/adopt` | 接手一个已有的 Claude Code 会话（导入最近 200 条记录） |
| GET | `/hub/sessions/{id}/events?after=&limit=` | 事件记录 |
| GET | `/hub/sessions/{id}/stream?after=` | SSE 实时事件，断线后用最后的 seq 重连补齐 |
| POST | `/hub/sessions/{id}/messages` | 发消息 `{text, request_id, delivery}`；正在跑时进队列，`delivery: "steer"` 插进这一轮 |
| DELETE | `/hub/sessions/{id}/queue/{qid}` | 撤回还没开始的排队消息 |
| POST | `/hub/sessions/{id}/mode` | 切换权限 `{mode}` |
| POST | `/hub/sessions/{id}/model` | 切换模型和思考强度 `{model, effort}` |
| POST | `/hub/sessions/{id}/stop` | 停止这一轮（队列随之暂停，下次发消息接着跑） |
| POST | `/hub/approvals/{req}` | 审批 `{allow, reason}` |
| POST | `/hub/questions/{qid}` | 回答提问 `{answers: {问题原文: 选项 label 或数组}}`，或 `{skip: true}` |
| GET | `/hub/stream` | 所有会话的状态、审批、提问、结果（列表页、通知用） |
| GET | `/hub/dirs?path=` | 选目录（只能选 home 下面的） |
| GET/POST/PATCH/DELETE | `/hub/workspaces…` | 工作空间 |

`/api/…` 是给 agentbox 前端的，格式以它的 `web/src/types.d.ts` 为准。

`request_id` 由客户端生成：网络超时可以原样重试，同一个 ID 只会执行一次；同一个 ID 换了内容返回 409。

## 已知限制

- 每轮起一个 Claude Code 进程；后台任务（`run_in_background`）会让这一轮等到它结束。
- 子 Agent 内部的步骤不展开，只显示那一次 Agent 调用和结果。
- 费用按 API 价目估算，订阅账号实际不按这个扣。
- 网页「同时运行的对话数」按空间限制（默认 3，可设 1–10）；原生 `/hub` / agentctl 保留自己的准入方式。
- 网页历史以 hub 追加事件为准。hub 开始原样存 Claude 事件（10-27 下午）之前的那段、以及接手前的终端记录，首次读取时从 Claude 转录冻结到 `events/<会话>.jsonl.history2.json`（跨压缩读全，旧段截图不留、工具输出截到 2000 字），CLI 后续压缩不会重排网页游标。删掉这个文件会按当时的转录重建一次。
- Windows 上可以跑（只开 HTTP，没有 agentctl 和终端），还没实测。

## 开发验证

不调用 Claude、全部状态写临时目录的 HTTP / WebSocket / PTY / 本地 Git 回归：

```sh
AGENT_HUB_TG=0 AGENT_HUB_MAX_TURNS=30 .venv/bin/python -m unittest discover -s hub/tests -v
```

可选浏览器回归（需要 Python Playwright 和 Chromium）：

```sh
AGENT_HUB_TG=0 .venv/bin/python hub/tests/browser_compat.py
```

浏览器回归运行原版网页与合成 CLI，覆盖发送、回合中刷新、工具行、文件 / Git / 技能页、设置保存、监控和窄屏；不代表真实 Claude 或真实 tmux 链路验收。协议差异与剩余验收见 [REVIEW-codex.md](docs/REVIEW-codex.md)。

## 额度消耗与自动压缩窗口（2026-10-30）

`usage.jsonl` 里 10-27～10-30 共 726 次请求，按 API 价折算约 $141。问题不在输出（只占约 8%），而在**上下文太长**：

* 单个会话（`homework`）上下文涨到约 **909K token**，每一步工具调用都要重读一遍；上下文超过 300K 的请求有 507 次，占了约 **88%** 的花费。
* 缓存约 1 小时过期。16 次「整段重写缓存」全部发生在同一会话**空闲超过 1 小时**之后，900K 的会话重写一次约 $4.5，合计约占 37%。
* Claude Code 的 `autoCompactWindow` 一直是 `auto`，Opus 5.5 在 `auto` 下几乎要到 1M 才压缩。

处理：`~/.claude/settings.json` 顶层加 `"autoCompactWindow": 300000`（官方设置项，100K～1M 或 `auto`；程序里「可能导致高用量」的提示只在设得比 `auto` 大时出现，往小设正是省额度的方向）。hub 每轮新起 Claude Code 进程，下一轮自动生效，不用重启服务。改前备份 `~/.claude/settings.json.bak-20261030-autocompact`。想调回去：删掉这一项，或在会话里用 `/autocompact`。

代价：长会话到 300K 会被压缩成摘要，早先的细节以摘要形式保留；需要完整细节的任务建议拆成新会话做。
