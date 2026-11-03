#!/usr/bin/env python3
"""
agent-hub 守护进程（2026-10-26）：在 lab 上用 Claude Agent SDK 跑 Claude Code 会话，给手机上的 Eta
提供原生的「远程会话」：会话列表、实时对话、审批、提问、排队、停止。对话协议参考了 agentbox
（devilcoolyue/agentbox）的持久接收、跟进排队和提问卡片，但 Agent 直接跑在宿主机上、每步可审批。

接口
- HTTP（给 Eta）：只听 127.0.0.1 和 lab 的 Tailscale 地址（AGENT_HUB_HTTP_HOSTS），端口 8765，
  请求头 Authorization: Bearer <~/.agent-hub/token>。增删改查是 JSON，实时事件走 SSE：
  /api/sessions/<id>/stream?after=<seq> 是单个会话，/api/stream 是所有会话的状态、审批和提问（列表页、通知用）。
- Unix socket（给 agentctl：本机调试、Telegram 按钮回调）：~/.agent-hub/hub.sock。Windows 上没有。

事件
每个会话一条事件流，存在 ~/.agent-hub/events/<id>.jsonl，seq 从 1 递增：user、assistant、tool_use、
tool_result、approval、approval_resolved、question、question_resolved、queued、dequeued、withdrawn、
status、result。逐字输出的 delta 只推不存，断线重连后由完整的 assistant 事件补上。

发消息
- 客户端每条消息带自己生成的 request_id：网络超时可以原样重试，hub 认出来就返回原结果，不会重复执行。
- 这一轮还在跑时发来的消息进队列，按顺序接着跑；还没开始的可以撤回。机主按了停止，队列先暂停，
  下次发消息时接着跑。
- 每次跑一轮起一个 ClaudeSDKClient：首轮用生成的 session_id 新建，之后 resume 同一个 Claude Code 会话。

审批和提问
- 工具要权限时（can_use_tool）发 approval 事件，同时推到机主 Telegram 私聊（用量机器人）带 ✅/❌ 按钮；
  Eta 里点、Telegram 里点、agentctl approve 都行，先到先算。
- Claude 用 AskUserQuestion 提问时发 question 事件，回答原样交回，同一轮接着做；单选的单个问题在
  Telegram 里也有选项按钮。
- 一轮结束时如果没有人开着这个会话看，就推一条结果到 Telegram（quiet 的不推）。

其它环境变量：AGENT_HUB_STATE、AGENT_HUB_TG=0（不发 Telegram）、AGENT_HUB_TG_CONFIG、AGENT_HUB_CLAUDE、
AGENT_HUB_APPROVAL_TIMEOUT / AGENT_HUB_QUESTION_TIMEOUT（秒）、AGENT_HUB_HTTP_PORT、AGENT_HUB_NAME、
AGENT_HUB_MAX_TURNS（一轮最多几步，默认 30）、AGENT_HUB_SYSTEM_APPEND（追加到系统提示后面的约定）。

费用：每轮花多少、会话累计多少，都按 ~/.agent-hub/usage.jsonl 的逐请求费用求和
（result.total_cost_usd 是 Claude Code 会话的累计值，不能按轮相加）。
"""
import asyncio
import collections
import errno
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import signal
import threading
import sys
import time
import traceback
import types
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field, fields

from aiohttp import hdrs, web
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    get_session_info,
    get_session_messages,
    list_sessions,
)
from claude_agent_sdk._internal.message_parser import parse_message

import hub_abx
import hub_files
import hub_git
import hub_optional
import hub_skills
import hub_spaces
import hub_term
import hub_workspaces

try:
    import fcntl
except ImportError:   # Windows：没有 fcntl，单实例靠 HTTP 端口占用兜底
    fcntl = None

STATE_DIR = os.path.expanduser(os.environ.get("AGENT_HUB_STATE", "~/.agent-hub"))
SOCK_PATH = os.path.join(STATE_DIR, "hub.sock")
SESSIONS_PATH = os.path.join(STATE_DIR, "sessions.json")
RECEIPTS_PATH = os.path.join(STATE_DIR, "receipts.json")
EVENTS_DIR = os.path.join(STATE_DIR, "events")
USAGE_PATH = os.path.join(STATE_DIR, "usage.jsonl")
TOKEN_PATH = os.path.join(STATE_DIR, "token")
TG_CONFIG = os.path.expanduser(
    os.environ.get("AGENT_HUB_TG_CONFIG", "~/ai-work/telegram-quota-monitor/config.env"))
TG_ENABLED = os.environ.get("AGENT_HUB_TG", "1") != "0"
CLAUDE_CLI = (os.environ.get("AGENT_HUB_CLAUDE") or shutil.which("claude")
              or os.path.expanduser("~/.npm-global/bin/claude"))
APPROVAL_TIMEOUT = int(os.environ.get("AGENT_HUB_APPROVAL_TIMEOUT", "1800"))
QUESTION_TIMEOUT = int(os.environ.get("AGENT_HUB_QUESTION_TIMEOUT", "1800"))
HTTP_HOSTS = [h.strip() for h in os.environ.get("AGENT_HUB_HTTP_HOSTS", "127.0.0.1,100.120.32.105").split(",")
              if h.strip()]
HTTP_PORT = int(os.environ.get("AGENT_HUB_HTTP_PORT", "8765"))
HUB_NAME = os.environ.get("AGENT_HUB_NAME", "lab")
HOME = os.path.realpath(os.path.expanduser("~"))
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
# agentbox 原版前端：fork 仓库里上游的编译产物 internal/web/static（跟上游合并就是升级前端）
ABX_DIR = os.path.realpath(os.environ.get("AGENT_HUB_ABX_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "internal", "web", "static"))
COOKIE = "agent_hub"

# bypassPermissions = 全部自动通过（2026-10-26 机主要的，不想每步点批准）；dontAsk 没意义，不开放
MODES = ("default", "acceptEdits", "plan", "auto", "bypassPermissions")
# 网页 / Eta 里能选的模型；空字符串 = 跟 Claude Code 自己的设置走（~/.claude/settings.json）
MODELS = [("", "默认"), ("claude-fable-5-1", "Fable 5.1"), ("claude-opus-5-5", "Opus 5.5"),
          ("claude-sonnet-5-5", "Sonnet 5.5"), ("claude-haiku-5-5", "Haiku 5.5")]
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
MODEL_ID = re.compile(r"^(claude-[a-z0-9.\-]{2,60}|opus|sonnet|haiku|fable|default)(\[1m\])?$")


def valid_model(model):
    return model == "" or bool(MODEL_ID.match(model))
ACTIVE_STATES = ("running", "waiting")
GLOBAL_KINDS = ("status", "approval", "approval_resolved", "question", "question_resolved", "result")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TG_TEXT_LIMIT = 3500
VIA = {"telegram": "Telegram", "agentctl": "agentctl", "eta": "Eta", "web": "网页", "timeout": "超时"}
HTTP_CLIENTS = ("eta", "web")   # HTTP 客户端自报来源，只认这两个


# 按 API 价估算每次请求的费用（美元 / 百万 token）：输入、输出、缓存读；缓存写按输入的 1.25 倍。
# 订阅账号实际不按这个扣，只用来比较哪个会话、哪个模型花得多。2026-10 的价目。
PRICES = {"claude-fable-5-1": (10, 50, 0.25), "claude-opus-5-5": (4, 20, 0.20), "claude-sonnet-5-5": (2, 10, 0.20),
          "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-opus-5": (5, 25, 0.50), "claude-fable-5": (10, 50, 1.0)}


def estimate_cost(row):
    model = row.get("model") or ""
    price = next((v for k, v in PRICES.items() if model.startswith(k)), None)
    if not price:
        return None
    inp, out, cread = price
    return round((row["input"] * inp + row["cache_write"] * inp * 1.25 + row["cache_read"] * cread
                  + row["output"] * out) / 1e6, 6)


def append_usage(row):
    with open(USAGE_PATH, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def env_int(name, default):
    """读整数环境变量：没设、写错或者 <=0 都用默认值。"""
    try:
        value = int((os.environ.get(name) or "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


# 一轮里 Claude 最多跑几步（工具调用算一步）。到点就停，并且暂停队列等人决定要不要接着跑，
# 免得一个回合自己无限接力、把额度烧光。原来没传这个参数 = 跟 CLI 默认（不限步数）。
MAX_TURNS = env_int("AGENT_HUB_MAX_TURNS", 30)
# 追加到 Claude Code 默认系统提示后面的一段约定（--append-system-prompt，不替换原提示）。
SYSTEM_APPEND = (os.environ.get("AGENT_HUB_SYSTEM_APPEND") or
                 "（hub 补充约定：用 Bash 时保持输出有界——看日志优先 tail -n 200 或者 grep/rg 过滤，"
                 "不要 cat 整个大文件；输出可能很长就先重定向到文件再分页读，单次不要返回几百行以上。）")


def load_usage_rows(path=None):
    """读 usage.jsonl；坏行跳过（历史文件里混了半行也不能把整份用量统计带崩）。"""
    rows = []
    try:
        with open(path or USAGE_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except FileNotFoundError:
        pass
    return rows


class UsageLedger:
    """usage.jsonl 的索引：按会话、按回合汇总费用。

    每行是 record_usage 记的一次模型请求。会话累计费用只认这里的求和：result.total_cost_usd 是
    Claude Code 会话从头到现在的累计值，按轮相加会把前面每一轮重复算一遍（2026-10-30 修复前
    sessions.json 里就是这样被放大几十倍的）。求和天然幂等：同一条用量只算一次。
    """

    def __init__(self, path=None):
        self.path = path or USAGE_PATH
        self.by_turn = {}     # (sid, turn) -> 费用
        self.by_sid = {}      # sid -> 费用
        self.last_turn = {}   # sid -> 最后一条用量所属的 turn id
        self.rows = 0
        self.reload()

    def reload(self):
        self.by_turn.clear()
        self.by_sid.clear()
        self.last_turn.clear()
        self.rows = 0
        for row in load_usage_rows(self.path):
            self.add(row)
        return self

    def add(self, row):
        sid = row.get("sid") or ""
        if not sid:
            return
        turn = row.get("turn") or ""
        cost = float(row.get("cost") or 0.0)
        self.by_turn[(sid, turn)] = self.by_turn.get((sid, turn), 0.0) + cost
        self.by_sid[sid] = self.by_sid.get(sid, 0.0) + cost
        self.last_turn[sid] = turn
        self.rows += 1

    def turn_cost(self, sid, turn):
        return self.by_turn.get((sid, turn or ""))

    def total(self, sid):
        return self.by_sid.get(sid)

    def has(self, sid):
        return sid in self.by_sid


def repair_sessions_costs(ledger, path=None, dry_run=False):
    """按 usage.jsonl 重算会话的 total_cost / last_cost；幂等，再跑一遍数值不变。

    只改这两个数字：不删会话、不动事件历史和用量明细。调用方负责加锁（服务停着的时候跑）。
    返回 (改动列表, 旧合计, 新合计)。
    """
    path = path or SESSIONS_PATH
    try:
        with open(path) as f:
            rows = json.load(f)
    except FileNotFoundError:
        return [], 0.0, 0.0
    if not isinstance(rows, list):
        return [], 0.0, 0.0
    changes, before, after = [], 0.0, 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("id") or ""
        old = float(row.get("total_cost") or 0.0)
        before += old
        new = ledger.total(sid)
        if new is None:   # 没有经过 hub 的用量记录（接手的老会话）：保持原样
            after += old
            continue
        old_last = row.get("last_cost")
        turn = ledger.last_turn.get(sid) or ""
        last = ledger.turn_cost(sid, turn)
        rec = {"id": sid, "name": row.get("name") or "", "old": round(old, 6), "new": round(new, 6),
               "old_last": old_last, "new_last": round(last, 6) if last is not None else None}
        if abs(old - new) > 1e-6:
            row["total_cost"] = round(new, 6)
            rec["changed"] = True
        try:
            last_off = old_last is None or abs(float(old_last) - last) > 1e-6
        except (TypeError, ValueError):
            last_off = True
        if last is not None and last_off:
            row["last_cost"] = round(last, 6)
            rec["changed"] = True
        if rec.get("changed"):
            changes.append(rec)
        after += float(row.get("total_cost") or 0.0)
    if changes and not dry_run:
        shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")   # 回滚用；只有会话索引
        write_json(path, rows)
    return changes, before, after


@dataclass
class Verdict:
    """一轮结果的结论：记成什么状态、错误写什么、要不要暂停队列。"""
    status: str
    error: str = ""
    pause_queue: bool = False
    mismatch: bool = False    # is_error=true 但没有失败证据：CLI 误报，按成功处理


API_ERROR_TEXT = {429: "被限流（HTTP 429）", 500: "服务端错误（HTTP 500）", 502: "网关错误（HTTP 502）",
                  503: "服务不可用（HTTP 503）", 529: "服务过载（HTTP 529）"}


def classify_result(result, max_turns=None):
    """这一轮结果算成功还是失败：只有证据清楚才算失败。

    SDK 的 is_error 在 subtype 还是 success 时也会是 True（CLI 报 API 错误，或者就是误报），原来
    直接按 `elif result.is_error` 判失败，于是「subtype=success、errors 为空」的回合也被记成 error，
    错误信息只剩 "success"（事件流里 13 条这种）。这里按证据分：
    - errors 有内容、api_error_status 有值、terminal_reason 是 api_error、subtype 以 error 开头 → 真失败；
    - 以上都没有、只有 is_error=True → 误报，按成功记，另打一行 stderr 方便复查。
    """
    sub = (getattr(result, "subtype", "") or "").strip()
    reason = (getattr(result, "terminal_reason", "") or "").strip()
    errors = [e for e in (getattr(result, "errors", None) or []) if e]
    api_status = getattr(result, "api_error_status", None)
    if reason in ("aborted_streaming", "aborted_tools"):
        return Verdict("interrupted", "被中断")
    if reason == "max_turns" or sub == "error_max_turns":
        limit = f"max_turns={max_turns}" if max_turns else "max_turns 上限"
        return Verdict("error", f"本轮达到 {limit}：Claude 已停下，队列已暂停。回复任意消息（网页 / agentctl "
                                f"send）就接着跑；想放宽上限设 AGENT_HUB_MAX_TURNS", True)
    if api_status is not None:
        try:
            text = API_ERROR_TEXT.get(int(api_status), f"（HTTP {api_status}）")
        except (TypeError, ValueError):
            text = f"（HTTP {api_status}）"
        return Verdict("error", f"Claude Code 报 API 错误{text}，这一轮没跑完")
    if errors:
        return Verdict("error", "; ".join(str(e) for e in errors)[:800])
    if reason == "api_error":
        return Verdict("error", "Claude Code 报 API 错误，这一轮没跑完")
    if sub.startswith("error"):
        return Verdict("error", f"执行失败（{sub}）")
    if "error" in reason:
        return Verdict("error", f"这一轮异常结束（{reason}）")
    if getattr(result, "is_error", False):
        return Verdict("idle", "", mismatch=True)
    return Verdict("idle", "")


def context_notice(percent):
    """上下文占用的提醒：>=50% 提前说一声，>=70% 明确建议压缩/新会话（hub 不自动压缩）。"""
    try:
        pct = float(percent if percent is not None else 0)
    except (TypeError, ValueError):
        return None, ""
    if pct >= 70:
        return "warn", (f"上下文已用到 {pct:.0f}%：再往下跑很快会触发自动压缩或者直接失败。建议先压缩"
                        f"（/compact）或者新建会话接着做；hub 不会自动替你压缩（压缩本身也花额度）。")
    if pct >= 50:
        return "info", f"上下文已用到 {pct:.0f}%，留意及时压缩或者新开会话。"
    return None, ""


@dataclass
class Turn:
    """一轮进行中的状态：后台命令、插话、每次模型请求的计时。"""
    sent_at: float
    id: str = ""                                      # 这一轮的 turn id（跟 user 事件里的一致，用量按它归到回合）
    result: object = None
    extra_results: list = field(default_factory=list)
    bg_tasks: dict = field(default_factory=dict)      # task_id → 描述
    steers: dict = field(default_factory=dict)        # 插话 uuid → (request_id, 状态)
    req: dict = field(default_factory=dict)           # parent_tool_use_id → 正在进行的模型请求
    thinking_emitted: float = 0.0
    user_interrupted: bool = False

    def steers_pending(self):
        return any(state != "applied" for _, state in self.steers.values())


class HubError(Exception):
    """报给客户端的错误：消息直接给人看。status 给 HTTP 用。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass
class Session:
    id: str                    # Claude Code 的会话 UUID
    name: str
    cwd: str
    mode: str = "default"
    status: str = "idle"       # idle / running / waiting / error / interrupted
    started: bool = False      # Claude 那边已经有会话文件：之后只能 resume，不能再按 session_id 新建
    adopted: bool = False      # 接手的已有会话，不是 hub 新建的
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    turns: int = 0
    last_prompt: str = ""
    last_result: str = ""
    last_error: str = ""
    last_cost: float | None = None
    total_cost: float = 0.0
    sdk_cost_total: float = 0.0   # 最近一次 SDK 报的「会话累计费用」，只用来算差值，不当累计用
    last_turn_id: str = ""        # 上一轮的 turn id（费用按回合归账）
    queue: list = field(default_factory=list)   # [{qid, text, quiet, created}]
    queue_paused: bool = False
    model: str = ""            # 空 = Claude Code 默认
    effort: str = ""           # 空 = 模型默认的思考强度
    actual_model: str = ""     # 上一轮 Claude Code 实际用的模型（init 里报的）
    commands: list = field(default_factory=list)   # 这个会话可用的 / 命令（init 里报的）
    command_info: list = field(default_factory=list)   # 带说明的 / 命令（server_info 里报的）
    title: str = ""            # 对话标题：默认是第一条消息
    skills: list = field(default_factory=list)
    context: dict = field(default_factory=dict)    # 上下文占用 {used, max, percent}
    task_tools: bool = False   # 打开 Claude Code 的任务清单工具（Claude 5 在 SDK 模式下默认关着，开了多花点 token）


@dataclass
class Waiter:
    """一个等机主处理的请求：工具审批（approval）或 Claude 的提问（question）。"""
    id: str
    sid: str
    kind: str
    tool: str
    summary: str               # 纯文本：Eta 卡片、agentctl pending
    reason: str
    path: str                  # 触发审批的文件路径（比如工作目录外），没有就空
    questions: list            # 提问的原始 questions
    html: str                  # Telegram 正文，处理后在后面追加结论
    created: float
    future: asyncio.Future
    tg_message_id: int | None = None


class EventLog:
    """一个会话的事件流：jsonl 落盘，订阅者是 SSE 连接的队列。"""

    def __init__(self, sid):
        self.path = os.path.join(EVENTS_DIR, f"{sid}.jsonl")
        self.subs: set[asyncio.Queue] = set()
        try:
            with open(self.path, "rb") as f:
                self.seq = sum(1 for _ in f)
        except FileNotFoundError:
            self.seq = 0

    def append(self, ev):
        with open(self.path, "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def read(self, after=0, limit=None):
        """返回的行是共享的缓存，调用方只读不改。"""
        rows = self.rows()
        if after:
            rows = [r for r in rows if r["seq"] > after]
        return rows[-limit:] if limit else rows

    def rows(self):
        """事件文件只追加：按偏移只解析新增的整行，长对话进行中反复读历史时不用每次重解析几十 MB。
        文件被删或变短（删会话后重建）就整份重读；没写完的半行留到下次。"""
        with _ROWS_LOCK:
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                _ROWS.pop(self.path, None)
                return []
            hit = _ROWS.get(self.path)
            if not hit or hit[0] != st.st_ino or st.st_size < hit[1]:
                hit = (st.st_ino, 0, [])
            ino, offset, rows = hit
            if st.st_size > offset:
                with open(self.path, "rb") as f:
                    f.seek(offset)
                    data = f.read(st.st_size - offset)
                end = data.rfind(b"\n") + 1
                if end:
                    # 新列表而不是原地追加：之前返回出去的列表不会被改动
                    # 只按 \n 切：splitlines 还会切在 U+2028 之类的字符上，事件正文里原样带着这些字符
                    rows = rows + [json.loads(line) for line in data[:end].split(b"\n") if line.strip()]
                    offset += end
            _ROWS[self.path] = (ino, offset, rows)
            _ROWS.move_to_end(self.path)
            while len(_ROWS) > ROWS_CACHED_LOGS:
                _ROWS.popitem(last=False)
            return rows


ROWS_CACHED_LOGS = 4   # 只缓存最近读过的几个会话
_ROWS: "collections.OrderedDict[str, tuple]" = collections.OrderedDict()
_ROWS_LOCK = threading.Lock()


def offer(q, ev):
    try:
        q.put_nowait(ev)
    except asyncio.QueueFull:
        # 客户端读得太慢：清空后放个 None 让它断开，重连时按 seq 补齐
        while not q.empty():
            q.get_nowait()
        q.put_nowait(None)


def clip(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字）"


def tool_summary(tool, inp):
    """把工具调用压成几行给人看：命令、文件路径、改动片段。"""
    if tool == "Bash":
        text = inp.get("command", "")
        if inp.get("description"):
            text = f"# {inp['description']}\n{text}"
    elif tool == "Write":
        text = f"{inp.get('file_path', '')}（{len(inp.get('content') or '')} 字符）"
    elif tool == "Edit":
        text = (f"{inp.get('file_path', '')}\n- {clip(inp.get('old_string'), 300)}"
                f"\n+ {clip(inp.get('new_string'), 300)}")
    elif tool in ("Read", "MultiEdit", "NotebookEdit"):
        text = inp.get("file_path") or inp.get("notebook_path") or json.dumps(inp, ensure_ascii=False)
    elif tool in ("Glob", "Grep"):
        text = " ".join(str(inp[k]) for k in ("pattern", "path") if inp.get(k))
    elif tool == "WebFetch":
        text = inp.get("url", "")
    elif tool == "TodoWrite":
        todos = inp.get("todos") or []
        done = sum(1 for t in todos if t.get("status") == "completed")
        text = f"任务 {done}/{len(todos)}"
    elif tool in ("Task", "Agent"):
        text = inp.get("description") or clip(inp.get("prompt"), 200)
    else:
        text = json.dumps(inp, ensure_ascii=False)
    return clip(text, 900)


def tool_detail(tool, inp):
    """Eta 渲染用的结构化输入：Edit 画 diff、TodoWrite 画任务列表、Bash 显示完整命令。"""
    s = lambda v, n: clip(v, n) if isinstance(v, str) else v   # noqa: E731
    if tool == "Edit":
        return {"file_path": inp.get("file_path"), "old_string": s(inp.get("old_string"), 3000),
                "new_string": s(inp.get("new_string"), 3000), "replace_all": bool(inp.get("replace_all"))}
    if tool == "MultiEdit":
        return {"file_path": inp.get("file_path"),
                "edits": [{"old_string": s(e.get("old_string"), 1500), "new_string": s(e.get("new_string"), 1500)}
                          for e in (inp.get("edits") or [])[:20]]}
    if tool == "Write":
        return {"file_path": inp.get("file_path"), "content": s(inp.get("content"), 4000)}
    if tool == "Bash":
        return {"command": s(inp.get("command"), 4000), "description": inp.get("description"),
                "run_in_background": bool(inp.get("run_in_background"))}
    if tool == "TodoWrite":
        return {"todos": [{"content": t.get("content"), "status": t.get("status"),
                           "activeForm": t.get("activeForm")} for t in (inp.get("todos") or [])[:50]]}
    if tool in ("Read", "Glob", "Grep", "WebFetch", "WebSearch"):
        return {k: s(v, 1000) for k, v in inp.items() if isinstance(v, (str, int, float, bool))}
    if tool in ("Task", "Agent"):
        return {"description": inp.get("description"), "subagent_type": inp.get("subagent_type"),
                "prompt": s(inp.get("prompt"), 1500)}
    return {"json": clip(json.dumps(inp, ensure_ascii=False), 2000)}


def result_text(content):
    if isinstance(content, list):
        return "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    return str(content or "")


def slim_event(data, limit=40000):
    """落盘前把过大的工具输出截短（读大文件、长命令输出），实时推送的那份不受影响。"""
    def cut(v):
        return v if not isinstance(v, str) or len(v) <= limit else v[:limit] + f"\n…（共 {len(v)} 字，后面省略）"
    if data.get("type") != "user":
        return data
    data = json.loads(json.dumps(data))
    for b in (data.get("message") or {}).get("content") or []:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            if isinstance(b.get("content"), str):
                b["content"] = cut(b["content"])
            elif isinstance(b.get("content"), list):
                for x in b["content"]:
                    if isinstance(x, dict) and "text" in x:
                        x["text"] = cut(x["text"])
    tur = data.get("tool_use_result")
    if isinstance(tur, dict):
        for k in ("stdout", "stderr", "content", "originalFile"):
            if k in tur:
                tur[k] = cut(tur[k]) if k != "originalFile" else None   # 原文件全文用不上，diff 有 structuredPatch
        if isinstance(tur.get("file"), dict) and "content" in tur["file"]:
            tur["file"]["content"] = cut(tur["file"]["content"])
    return data


def tool_use_event(name, tool_id, inp):
    return {"id": tool_id, "name": name, "summary": tool_summary(name, inp), "detail": tool_detail(name, inp)}


def transcript_events(msgs):
    """Claude Code 的会话记录 → 和实时事件同样的 (kind, data)，接手已有会话时导入用。"""
    out = []
    for m in msgs:
        content = (m.message or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for b in blocks:
            kind = b.get("type")
            if kind == "text" and (b.get("text") or "").strip():
                out.append(("user" if m.type == "user" else "assistant", {"text": b["text"]}))
            elif kind == "tool_use":
                out.append(("tool_use", tool_use_event(b.get("name"), b.get("id"), b.get("input") or {})))
            elif kind == "tool_result":
                out.append(("tool_result", {"id": b.get("tool_use_id"), "text": clip(result_text(b.get("content")), 2000),
                                            "is_error": bool(b.get("is_error"))}))
    return out


def digest(**fields_):
    return hashlib.sha256(json.dumps(fields_, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fmt_secs(secs):
    secs = int(secs)
    return f"{secs // 60} 分 {secs % 60} 秒" if secs >= 60 else f"{secs} 秒"


def load_tg():
    if not TG_ENABLED:
        return None
    cfg = {}
    try:
        with open(TG_CONFIG) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except OSError as e:
        print(f"telegram config unreadable ({type(e).__name__}); notifications off", file=sys.stderr, flush=True)
        return None
    token, chat = cfg.get("TELEGRAM_USAGE_BOT_TOKEN"), cfg.get("TELEGRAM_USAGE_CHAT_ID")
    if not token or not chat:
        print("telegram config lacks the usage bot token/chat; notifications off", file=sys.stderr, flush=True)
        return None
    return token, chat


def tg_call(token, method, payload):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def load_token():
    try:
        with open(TOKEN_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        return token


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class Hub:
    valid_model = staticmethod(valid_model)
    slim_event = staticmethod(slim_event)

    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.logs: dict[str, EventLog] = {}
        self.active: dict[str, ClaudeSDKClient] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.waiters: dict[str, Waiter] = {}
        self.receipts: dict[str, dict] = {}            # request_id → {sid, digest, queued, created}
        self.changed: dict[str, asyncio.Event] = {}   # 状态一变就 set，agentctl wait 靠它醒来
        self.global_subs: set[asyncio.Queue] = set()
        self.turns: dict[str, Turn] = {}
        self.listeners: list = []
        self.rate_limit = None          # 最近一次 rate_limit_event：5 小时 / 7 天窗口用了多少
        self.stopping = False
        self.tg = load_tg()
        self.ledger = UsageLedger(USAGE_PATH)   # 费用账本：累计和每轮费用都按 usage.jsonl 求和

    # ---- 登记、回执和事件 ----

    def load(self):
        try:
            with open(SESSIONS_PATH) as f:
                rows = json.load(f)
        except FileNotFoundError:
            rows = []
        known = {f.name for f in fields(Session)}
        for row in rows:
            s = Session(**{k: v for k, v in row.items() if k in known})
            if s.status in ACTIVE_STATES:   # 上次 hub 退出时这一轮还在跑，Claude 进程已经跟着没了
                s.status, s.last_error = "interrupted", "agent-hub 重启，这一轮没做完"
            self.sessions[s.id] = s
        try:
            with open(RECEIPTS_PATH) as f:
                self.receipts = json.load(f)
        except FileNotFoundError:
            pass

    def save(self):
        write_json(SESSIONS_PATH, [asdict(s) for s in self.sessions.values()])

    def remember(self, rid, sid, dig, queued=None):
        if not rid:
            return
        self.receipts[rid] = {"sid": sid, "digest": dig, "queued": queued, "created": time.time()}
        if len(self.receipts) > 2000:   # 只留最近的，够覆盖手机重试的时间窗
            for k, _ in sorted(self.receipts.items(), key=lambda kv: kv[1]["created"])[:500]:
                del self.receipts[k]
        write_json(RECEIPTS_PATH, self.receipts)

    def replay(self, rid, dig):
        """同一个 request_id 再来一次：内容一样就返回原结果，不一样就是客户端的错。"""
        if not rid:
            return None
        if not REQUEST_ID.match(rid):
            raise HubError("request_id 只能是 8–64 位字母、数字、- 或 _")
        r = self.receipts.get(rid)
        if r is None:
            return None
        if r["digest"] != dig:
            raise HubError("这个 request_id 已经用来发过别的内容", status=409)
        s = self.sessions.get(r["sid"])
        if s is None:
            raise HubError("这个 request_id 对应的会话已经不在了", status=409)
        return {"session": self.view(s), "replayed": True, "queued": r.get("queued")}

    def log_of(self, sid):
        if sid not in self.logs:
            self.logs[sid] = EventLog(sid)
        return self.logs[sid]

    def emit(self, s, kind, persist=True, **data):
        log = self.log_of(s.id)
        ev = {"kind": kind, "ts": round(time.time(), 3), **data}
        if persist:
            log.seq += 1
            ev = {"seq": log.seq, **ev}
            log.append(ev)
        for q in list(log.subs):
            offer(q, ev)
        if kind in GLOBAL_KINDS:
            gev = {**{k: v for k, v in ev.items() if k != "seq"}, "sid": s.id, "name": s.name}
            for q in list(self.global_subs):
                offer(q, gev)
        for fn in self.listeners:   # agentbox 兼容层（hub_abx）把事件转成它的对话通道消息
            try:
                fn(s, ev)
            except Exception:
                traceback.print_exc()

    def broadcast(self, kind, **data):
        """不属于某个会话的全局事件（工作空间变动之类），只推 /api/stream。"""
        ev = {"kind": kind, "ts": round(time.time(), 3), **data}
        for q in list(self.global_subs):
            offer(q, ev)

    def touch(self, s, **changes):
        status_before = s.status
        for k, v in changes.items():
            setattr(s, k, v)
        s.updated = time.time()
        self.save()
        if s.status != status_before:
            self.emit(s, "status", status=s.status, error=s.last_error if s.status == "error" else "")
            self.changed.pop(s.id, asyncio.Event()).set()

    def view(self, s, full=False):
        return {
            "id": s.id, "name": s.name, "cwd": s.cwd, "mode": s.mode, "status": s.status,
            "model": s.model, "effort": s.effort, "actual_model": s.actual_model,
            "commands": s.commands, "context": s.context,
            "adopted": s.adopted, "turns": s.turns, "created": s.created, "updated": s.updated,
            "last_cost": s.last_cost, "total_cost": round(s.total_cost, 4),
            "last_prompt": clip(s.last_prompt, 200),
            "last_result": s.last_result if full else clip(s.last_result, 300),
            "preview": clip(s.last_result or s.last_prompt, 120),
            "last_error": s.last_error,
            "seq": self.log_of(s.id).seq,
            "queue": [{"qid": q["qid"], "text": clip(q["text"], 300), "created": q["created"]} for q in s.queue],
            "queue_paused": s.queue_paused,
            "pending": [self.waiter_view(w) for w in self.waiters_of(s) if w.kind == "approval"],
            "questions": [self.waiter_view(w) for w in self.waiters_of(s) if w.kind == "question"],
        }

    def waiter_view(self, w):
        s = self.sessions.get(w.sid)
        d = {"req" if w.kind == "approval" else "qid": w.id, "kind": w.kind, "sid": w.sid,
             "session": s.name if s else w.sid[:8], "age": int(time.time() - w.created)}
        if w.kind == "approval":
            d.update(tool=w.tool, summary=w.summary, reason=w.reason, path=w.path)
        else:
            d.update(questions=w.questions)
        return d

    def waiters_of(self, s):
        return [w for w in self.waiters.values() if w.sid == s.id]

    def resolve(self, ref):
        ref = (ref or "").strip()
        if not ref:
            raise HubError("缺少会话 ID")
        hits = [s for s in self.sessions.values() if s.id.startswith(ref) or s.name == ref]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise HubError(f"「{ref}」匹配到 {len(hits)} 个会话，多给几位 ID")
        raise HubError(f"agent-hub 里没有会话「{ref}」", status=404)

    async def find_claude_session(self, ref):
        infos = await asyncio.to_thread(list_sessions, limit=300)
        hits = [i for i in infos if i.session_id.startswith(ref)]
        if len(hits) != 1:
            raise HubError(f"找不到会话「{ref}」" if not hits else f"「{ref}」匹配到多个会话，多给几位 ID",
                           status=404 if not hits else 400)
        return hits[0]

    async def adopt(self, ref):
        """接手 lab 上已有的 Claude Code 会话（比如在终端里开的）：登记进 hub，导入最近的记录。"""
        try:
            return self.resolve(ref)
        except HubError:
            if len((ref or "").strip()) < 4:
                raise
        info = await self.find_claude_session(ref.strip())
        if not info.cwd or not os.path.isdir(info.cwd):
            raise HubError("这个会话没有记录可用的工作目录，没法接手")
        s = Session(id=info.session_id, name=clip(info.custom_title or info.summary or info.session_id[:8], 40),
                    cwd=info.cwd, started=True, adopted=True,
                    updated=info.last_modified / 1000, created=(info.created_at or info.last_modified) / 1000)
        self.sessions[s.id] = s
        self.save()
        self.workspaces.ensure(s.cwd)
        if self.log_of(s.id).seq == 0:
            msgs = await asyncio.to_thread(get_session_messages, s.id, directory=s.cwd)
            for kind, data in transcript_events(msgs)[-200:]:
                self.emit(s, kind, imported=True, **data)
        return s

    # ---- 跑一轮、排队 ----

    def start_turn(self, s, prompt, quiet, qid=None, turn_id=None):
        tid = turn_id or secrets.token_hex(6)
        self.emit(s, "user", text=prompt, qid=qid,
                  turn={"id": tid, "model": s.model or s.actual_model, "effort": s.effort, "control": "effort"})
        self.touch(s, status="running", last_prompt=prompt, last_error="", queue_paused=False)
        task = asyncio.create_task(self.run_turn(s, prompt, quiet, tid))
        self.tasks[s.id] = task
        task.add_done_callback(lambda t, s=s: self.turn_done(s, t))

    def turn_done(self, s, task):
        if self.tasks.get(s.id) is task:
            del self.tasks[s.id]
            self.changed.pop(s.id, asyncio.Event()).set()   # agentctl wait 看的是任务还在不在，这里再叫醒一次
        if not self.stopping and not s.queue_paused:
            self.run_next(s)
        if hasattr(self, "abx"):
            self.abx.kick_queue()

    def run_next(self, s):
        if s.id in self.tasks or not s.queue:
            return
        if hasattr(self, "abx"):
            w = self.workspaces.by_path(s.cwd)
            if w and (w.get("stopped") or self.abx.blocked(w["id"], s.id)):
                return
        item = s.queue.pop(0)
        self.save()
        self.emit(s, "dequeued", qid=item["qid"])
        self.start_turn(s, item["text"], item.get("quiet", False), qid=item["qid"], turn_id=item.get("turn_id"))

    def turn_options(self, s):
        """本轮起 Claude Code 用的参数（拆出来便于单测：不连模型也能查参数）。"""
        # allow-dangerously…：只是允许中途切到 bypassPermissions，是否跳过审批仍按 permission_mode；
        # replay-user-messages：插话生效时 Claude Code 会回显那条消息，用来标「已插入」；
        # append-system-prompt：只往默认系统提示后面追加一段「输出要有界」的约定，不替换原提示、不动权限；
        # prompt-suggestions 是可选的多问一次「下一步写什么」的额外请求，已经关掉（省额度）。
        extra = {"allow-dangerously-skip-permissions": None, "replay-user-messages": None}
        if SYSTEM_APPEND:
            extra["append-system-prompt"] = SYSTEM_APPEND
        return ClaudeAgentOptions(
            cwd=s.cwd,
            permission_mode=s.mode,
            model=s.model or None,
            effort=s.effort or None,
            can_use_tool=self.permission_handler(s),
            include_partial_messages=True,
            thinking={"type": "adaptive", "display": "summarized"},   # 页面上显示思考摘要；只影响显示，不影响计费
            extra_args=extra,
            max_turns=MAX_TURNS,   # 一轮最多 30 步（AGENT_HUB_MAX_TURNS）：到点停下并暂停队列
            cli_path=CLAUDE_CLI,
            env={"CLAUDE_CODE_ENABLE_TODO_TOOLS": "1"} if s.task_tools else {},
            stderr=lambda line: line.strip() and print(f"[{s.id[:8]}] {line}", file=sys.stderr, flush=True),
            **({"resume": s.id} if s.started else {"session_id": s.id}),
        )

    async def run_turn(self, s, prompt, quiet, turn_id=""):
        turn = Turn(sent_at=time.time(), id=turn_id)
        self.turns[s.id] = turn
        client = None
        exit_failed = False
        t0 = time.time()
        try:
            if not s.started and await asyncio.to_thread(get_session_info, s.id, directory=s.cwd):
                # 上次已建好 CLI 会话但还没收到 init：沿用原 ID 续聊。
                self.touch(s, started=True)
            client = ClaudeSDKClient(self.turn_options(s))
            self.active[s.id] = client
            await client.connect()
            if not s.command_info:
                try:
                    info = await asyncio.wait_for(client.get_server_info(), 10) or {}
                    s.command_info = [{"name": c.get("name"), "description": c.get("description") or "",
                                       "argument_hint": c.get("argumentHint") or ""}
                                      for c in info.get("commands") or [] if c.get("name")]
                except Exception as e:
                    print(f"get_server_info failed: {type(e).__name__}", file=sys.stderr, flush=True)
            await client.query(prompt)
            await self.pump(s, client, turn)
            if turn.result is not None:
                await self.capture_context(s, client)
        except asyncio.CancelledError:
            self.touch(s, status="interrupted", last_error="" if turn.user_interrupted else "agent-hub 停止，这一轮没做完")
        except Exception as e:
            traceback.print_exc()
            exit_failed = isinstance(e, ProcessError) and e.exit_code not in (None, 0)
            self.touch(s, status="error", last_error=clip(f"{type(e).__name__}: {e}", 800))
        finally:
            self.active.pop(s.id, None)
            self.turns.pop(s.id, None)
            for w in self.waiters_of(s):
                if not w.future.done():
                    w.future.set_result(None)
            for uid, (rid, state) in turn.steers.items():
                if state != "applied":   # 回合在插话生效前就结束了：告诉页面，让机主决定要不要再发
                    self.emit(s, "steer", qid=rid, state="dropped")
            try:
                if client is not None:
                    await client.disconnect()
            except Exception:
                traceback.print_exc()

        result = turn.result
        if result is not None:
            verdict = classify_result(result, MAX_TURNS)
            if verdict.mismatch:
                print(f"[{s.id[:8]}] result is_error=true 但没有失败证据（subtype={result.subtype}），按成功处理",
                      file=sys.stderr, flush=True)
            last_cost, total_cost = self.turn_cost(s, turn, result)
            text = "\n\n".join(x for x in [result.result or "", *turn.extra_results] if x)
            if verdict.pause_queue:   # 到 max_turns 上限：先把队列停下，别自动接下一轮
                self.touch(s, queue_paused=True)
            self.touch(s, status=verdict.status, last_result=text, last_error=verdict.error,
                       last_cost=last_cost, total_cost=total_cost, turns=s.turns + 1, started=True,
                       last_turn_id=turn.id,
                       sdk_cost_total=getattr(result, "total_cost_usd", None) or s.sdk_cost_total)
            if verdict.pause_queue:
                self.emit(s, "notice", level="warn", reason="max_turns", text=verdict.error)
        elif s.status in ACTIVE_STATES:   # 没有结果也没有异常：Claude Code 进程中途退出
            self.touch(s, status="error", last_error="Claude Code 没给结果就退出了")
        secs = time.time() - t0
        if turn.user_interrupted:
            self.touch(s, status="interrupted", last_error="")
        uncertain = "" if turn.user_interrupted else "server_shutdown" if self.stopping else (
            "" if result is not None or exit_failed else "execution_incomplete")
        self.emit(s, "result", status=s.status, text=clip(s.last_result, 4000), error=s.last_error,
                  cost=s.last_cost if result else None, steps=result.num_turns if result else 0,
                  secs=round(secs, 1), **({"error_code": uncertain} if uncertain else {}))
        if not quiet and not self.log_of(s.id).subs:   # 有人开着这个会话在看，就不再推 Telegram
            await self.notify_result(s, result, secs)

    def turn_cost(self, s, turn, result=None):
        """这一轮多少钱、会话累计多少钱：都按 usage.jsonl 的逐请求费用求和。

        优先用「这个回合」的用量行求和；这个回合一行都没有（没走 hub 的 usage 记录）才退回
        SDK 累计值的差（本次累计 - 上次记下的累计），不会把前面轮次重复加进累计。
        """
        cost = self.ledger.turn_cost(s.id, turn.id)
        cum = getattr(result, "total_cost_usd", None) if result is not None else None
        delta = 0.0
        if cum is not None:
            try:
                delta = max(0.0, float(cum) - float(getattr(s, "sdk_cost_total", 0.0) or 0.0))
            except (TypeError, ValueError):
                delta = 0.0
        if cost is not None:
            return round(cost, 6), round(self.ledger.total(s.id) or 0.0, 6)
        if self.ledger.has(s.id):
            return (round(delta, 6) if delta > 1e-9 else None), round(self.ledger.total(s.id) or 0.0, 6)
        if delta > 1e-9:
            return round(delta, 6), round(float(s.total_cost or 0.0) + delta, 6)
        return None, float(s.total_cost or 0.0)

    async def pump(self, s, client, turn):
        """读 Claude Code 的原始消息流（SDK 会丢掉它不认识的类型，比如插话回执、下一步提示）。

        我们这条消息的结果到了还不一定能收工：Claude 起了后台命令、或者有插话还没生效时，
        Claude Code 会在它们结束后接着答，进程得留着，直到再来一个结果。"""
        stream = client._query.receive_messages().__aiter__()   # 原始 dict；SDK 版本在 requirements.txt 里锁定
        while True:
            if turn.result is None:
                wait = None
            elif turn.bg_tasks or turn.steers_pending():
                wait = 3600   # 后台命令最多再等一小时
            else:
                wait = 120    # 后台命令都结束了，等 Claude 接着答的那一轮
            try:
                data = await (asyncio.wait_for(stream.__anext__(), wait) if wait else stream.__anext__())
            except (StopAsyncIteration, TimeoutError):
                return
            t = data.get("type")
            parent = data.get("parent_tool_use_id")
            self.forward_raw(s, t, data)
            if t == "result":
                human = (data.get("origin") or {}).get("kind", "human") == "human"
                if human or turn.result is not None:
                    turn.result = parse_message(data) if human or turn.result is None else turn.result
                    if not human:   # 后台命令结束后 Claude 接着答的那一轮：结果文字接到这一轮后面
                        turn.extra_results.append(data.get("result") or "")
                    if not turn.bg_tasks and not turn.steers_pending():
                        return
                    self.emit(s, "bg_wait", tasks=list(turn.bg_tasks.values()))
                # resume 时插进来的通知轮（上一轮后台命令被停掉之类），不算这一轮的结果
                continue
            if t == "stream_event":
                self.on_stream_event(s, turn, data.get("event") or {}, parent)
            elif t == "system":
                self.on_system(s, turn, data)
            elif t == "assistant":
                if data.get("error"):
                    continue
                for b in (data.get("message") or {}).get("content") or []:
                    kind = b.get("type")
                    if kind == "text" and (b.get("text") or "").strip():
                        self.emit(s, "assistant", text=b["text"], parent=parent)
                    elif kind == "thinking" and (b.get("thinking") or "").strip():
                        self.emit(s, "thinking", text=b["thinking"], parent=parent)
                    elif kind == "tool_use" and b.get("name") != "AskUserQuestion":   # 提问另有 question 事件
                        self.emit(s, "tool_use", parent=parent, **tool_use_event(b.get("name"), b.get("id"),
                                                                                 b.get("input") or {}))
            elif t == "user":
                self.on_user(s, turn, data, parent)
            elif t == "command_lifecycle":
                rid_state = turn.steers.get(data.get("command_uuid"))
                if rid_state and rid_state[1] != "applied" and data.get("state") in ("queued", "started"):
                    state = "applied" if data["state"] == "started" else "queued"
                    turn.steers[data["command_uuid"]] = (rid_state[0], state)
                    self.emit(s, "steer", qid=rid_state[0], state=state)
                elif rid_state and data.get("state") == "completed":
                    turn.steers[data["command_uuid"]] = (rid_state[0], "applied")
            elif t == "prompt_suggestion":
                text = data.get("suggestion") or data.get("prompt") or data.get("text") or ""
                if text:
                    self.emit(s, "suggestion", text=text)
            elif t == "rate_limit_event":
                self.rate_limit = {**(data.get("rate_limit_info") or {}), "at": time.time()}

    def forward_raw(self, s, t, data):
        """原样转发 Claude Code 的事件给网页（agentbox 的渲染模块吃这个格式）：完整事件落盘，逐字增量只推。"""
        if t == "command_lifecycle" or (
                t == "system" and data.get("subtype") == "thinking_tokens") or (
                t == "user" and data.get("isReplay")):
            return
        if t == "stream_event":
            self.emit(s, "agent_event", persist=False, event=data)
        else:
            stopped = t == "result" and getattr(self.turns.get(s.id), "user_interrupted", False)
            self.emit(s, "agent_event", event=slim_event(data), **({"state": "interrupted"} if stopped else {}))

    def on_stream_event(self, s, turn, ev, parent):
        kind = ev.get("type")
        if kind == "message_start":
            msg = ev.get("message") or {}
            turn.req[parent] = {"start": time.time(), "first": None, "model": msg.get("model"),
                                "usage": dict(msg.get("usage") or {})}
        elif kind == "content_block_delta":
            r = turn.req.get(parent)
            if r and r["first"] is None:
                r["first"] = time.time()
            delta = ev.get("delta") or {}
            if parent is None and delta.get("type") == "text_delta":
                self.emit(s, "delta", persist=False, text=delta.get("text", ""))
            elif parent is None and delta.get("type") == "thinking_delta":
                self.emit(s, "thinking_delta", persist=False, text=delta.get("thinking", ""))
        elif kind == "message_delta":
            r = turn.req.get(parent)
            if r:
                r["usage"].update(ev.get("usage") or {})
        elif kind == "message_stop":
            r = turn.req.pop(parent, None)
            if r:
                self.record_usage(s, turn, r, parent)

    def record_usage(self, s, turn, r, parent):
        """每一次模型请求记一行：token、缓存、首字延迟、耗时、按 API 价估的费用（订阅账号实际不按这个扣）。"""
        u = r["usage"]
        now = time.time()
        since = turn.sent_at if parent is None else r["start"]
        row = {"ts": round(now, 3), "sid": s.id, "session": s.name, "cwd": s.cwd, "turn": turn.id, "model": r["model"],
               "sub": bool(parent), "effort": s.effort,
               "input": u.get("input_tokens") or 0, "output": u.get("output_tokens") or 0,
               "cache_read": u.get("cache_read_input_tokens") or 0,
               "cache_write": u.get("cache_creation_input_tokens") or 0,
               "ttft_ms": round(((r["first"] or now) - since) * 1000),
               "dur_ms": round((now - since) * 1000)}
        row["cost"] = estimate_cost(row)
        gen = now - (r["first"] or now)
        if row["output"] and gen > 0.3:   # 输出一次性到达时算不出速度，就不记
            row["tps"] = round(row["output"] / gen, 1)
        append_usage(row)
        self.ledger.add(row)
        self.emit(s, "usage", persist=False, **{k: row[k] for k in ("model", "input", "output", "cache_read", "cost")})

    def on_system(self, s, turn, data):
        sub = data.get("subtype")
        if sub == "init":
            self.touch(s, started=True, actual_model=data.get("model") or "",
                       commands=sorted(set(data.get("slash_commands") or []) - set(data.get("terminal_slash_commands") or [])),
                       skills=data.get("skills") or [])
        elif sub == "task_started" and data.get("is_backgrounded"):
            turn.bg_tasks[data.get("task_id")] = data.get("description") or "后台命令"
            self.emit(s, "task", id=data.get("task_id"), state="started", desc=data.get("description") or "",
                      bg=True)
        elif sub == "task_notification":
            desc = turn.bg_tasks.pop(data.get("task_id"), None)
            if desc is not None:
                self.emit(s, "task", id=data.get("task_id"), state=data.get("status") or "completed", desc=desc,
                          bg=True)
        elif sub == "thinking_tokens" and data.get("parent_tool_use_id") is None:
            if time.time() - turn.thinking_emitted > 1:   # 一秒最多一次，页面上显示「思考中 · N tokens」
                turn.thinking_emitted = time.time()
                self.emit(s, "thinking_tokens", persist=False, n=data.get("estimated_tokens") or 0)

    def on_user(self, s, turn, data, parent):
        if data.get("isReplay"):
            rid_state = turn.steers.get(data.get("uuid"))
            if rid_state and rid_state[1] != "applied":
                turn.steers[data["uuid"]] = (rid_state[0], "applied")
                self.emit(s, "steer", qid=rid_state[0], state="applied")
            return
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        meta = data.get("tool_use_result") if isinstance(data.get("tool_use_result"), dict) else {}
        for b in content:
            if b.get("type") != "tool_result":
                continue
            extra = {}
            if meta.get("structuredPatch"):   # Edit / Write 改动：带行号的补丁，页面照 CLI 的样子画 diff
                extra["patch"] = [{**h, "lines": h.get("lines", [])[:400]} for h in meta["structuredPatch"][:30]]
            elif meta.get("type") == "create" and meta.get("content") is not None:
                extra["created"] = clip(meta.get("content"), 4000)
            self.emit(s, "tool_result", id=b.get("tool_use_id"), text=clip(result_text(b.get("content")), 4000),
                      is_error=bool(b.get("is_error")), parent=parent, **extra)
        if parent is None:
            turn.sent_at = time.time()   # 工具结果交回去之后，下一次模型请求从这里开始算延迟

    async def capture_context(self, s, client):
        """回合结束、进程还没关时问一下上下文占用：页面上的圆环和 /context 格子图用。"""
        try:
            cu = await asyncio.wait_for(client.get_context_usage(), 10)
        except Exception as e:
            print(f"get_context_usage failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            return
        ctx = {"used": cu.get("totalTokens"), "max": cu.get("maxTokens"), "raw_max": cu.get("rawMaxTokens"),
               "percent": round(cu.get("percentage") or 0, 1), "model": cu.get("model"),
               "categories": [{"name": c.get("name"), "tokens": c.get("tokens"), "color": c.get("color")}
                              for c in cu.get("categories") or []],
               "grid": cu.get("gridRows") or []}
        self.touch(s, context={k: ctx[k] for k in ("used", "max", "percent")})
        self.emit(s, "context", **ctx)
        level, note = context_notice(ctx.get("percent"))
        if note:   # 只提醒，不擅自跑压缩（压缩本身要花额度）
            self.emit(s, "notice", level=level, reason="context", percent=ctx.get("percent"), text=note)

    async def steer(self, s, text, rid):
        """插话：把消息写进正在跑的这一轮，Claude Code 在当前这步工具做完后接收（priority=next）。"""
        client, turn = self.active.get(s.id), self.turns.get(s.id)
        if not client or not turn:
            return False
        uid = str(uuid.UUID(hashlib.md5(rid.encode()).hexdigest()))
        turn.steers[uid] = (rid, "sent")
        try:
            await client._transport.write(json.dumps({
                "type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None,
                "session_id": "default", "priority": "next", "uuid": uid}) + "\n")
        except BaseException:
            turn.steers.pop(uid, None)
            raise
        self.emit(s, "steer", qid=rid, text=text, state="sent")
        return True

    # ---- 审批和提问 ----

    def new_waiter_id(self):
        wid = secrets.token_hex(3)
        while wid in self.waiters:
            wid = secrets.token_hex(3)
        return wid

    async def wait_for_owner(self, s, w, timeout):
        """登记、推送，然后等机主处理；返回 decide()/answer() 交过来的结果，超时或这一轮结束返回 None。"""
        self.waiters[w.id] = w
        self.touch(s, status="waiting")
        try:
            return await asyncio.wait_for(asyncio.shield(w.future), timeout)
        except TimeoutError:
            note = f"{timeout // 60} 分钟没人处理，" + ("自动拒绝" if w.kind == "approval" else "让 Claude 自己判断")
            self.emit(s, f"{w.kind}_resolved", **{"req" if w.kind == "approval" else "qid": w.id},
                      allow=False, state="expired", via="timeout", reason=note)
            await self.tg_edit(w, "⌛ " + note)
            return None
        finally:
            self.waiters.pop(w.id, None)
            if s.status == "waiting" and s.id in self.active and not self.waiters_of(s):
                self.touch(s, status="running")

    def permission_handler(self, s):
        async def can_use_tool(tool, tool_input, ctx):
            if tool == "AskUserQuestion":
                return await self.ask_owner(s, tool_input)
            wid = self.new_waiter_id()
            summary = tool_summary(tool, tool_input)
            reason, path = ctx.decision_reason or "", ctx.blocked_path or ""
            lines = [f"🔐 <b>{html.escape(s.name)}</b> 请求使用 <code>{html.escape(tool)}</code>",
                     f"<pre>{html.escape(summary)}</pre>"]
            if reason:
                lines.append(f"原因：{html.escape(reason)}")
            if path:
                lines.append(f"路径：<code>{html.escape(path)}</code>")
            lines.append(f"会话 <code>{s.id[:8]}</code> · 请求 <code>{wid}</code>")
            w = Waiter(id=wid, sid=s.id, kind="approval", tool=tool, summary=summary, reason=reason, path=path,
                       questions=[], html="\n".join(lines), created=time.time(),
                       future=asyncio.get_running_loop().create_future())
            self.emit(s, "approval", req=wid, tool=tool, summary=summary, reason=reason, path=path,
                      detail=tool_detail(tool, tool_input))
            # 审批总是推 Telegram：手机锁屏时 Eta 的连接不一定还在，按钮在 Telegram 里也能点
            w.tg_message_id = await self.tg_send(w.html, buttons=[[("✅ 批准", f"ah:a:{wid}"),
                                                                 ("❌ 拒绝", f"ah:d:{wid}")]])
            decision = await self.wait_for_owner(s, w, APPROVAL_TIMEOUT)
            if decision and decision[0]:
                return PermissionResultAllow(updated_input=tool_input)
            note = decision[1] if decision else ""
            return PermissionResultDeny(message=note or "机主没有批准这个操作")
        return can_use_tool

    async def ask_owner(self, s, tool_input):
        questions = tool_input.get("questions") or []
        wid = self.new_waiter_id()
        lines = [f"❓ <b>{html.escape(s.name)}</b> 想问你："]
        for q in questions:
            lines.append(f"\n<b>{html.escape(q.get('header') or '')}</b> {html.escape(q.get('question') or '')}"
                         + ("（可多选）" if q.get("multiSelect") else ""))
            for i, o in enumerate(q.get("options") or [], 1):
                desc = f" — {o['description']}" if o.get("description") else ""
                lines.append(f"  {i}. {html.escape(o.get('label') or '')}{html.escape(desc)}")
        lines.append(f"\n会话 <code>{s.id[:8]}</code> · 问题 <code>{wid}</code>")
        w = Waiter(id=wid, sid=s.id, kind="question", tool="AskUserQuestion", summary="", reason="", path="",
                   questions=questions, html="\n".join(lines), created=time.time(),
                   future=asyncio.get_running_loop().create_future())
        self.emit(s, "question", qid=wid, questions=questions)
        buttons = None
        if len(questions) == 1 and not questions[0].get("multiSelect"):
            # 单选的单个问题在 Telegram 里直接点；其它情况到 Eta 里答
            opts = questions[0].get("options") or []
            buttons = [[(clip(o.get("label"), 30), f"ah:q:{wid}:{i}")] for i, o in enumerate(opts)]
            buttons.append([("跳过，让 Claude 自己判断", f"ah:s:{wid}")])
        w.tg_message_id = await self.tg_send(w.html, buttons=buttons)
        reply = await self.wait_for_owner(s, w, QUESTION_TIMEOUT)
        if not reply or reply.get("skip"):
            return PermissionResultDeny(message="机主没有回答这个问题。请按你自己的判断继续，并说明你做了哪些假设。")
        updated = {"questions": questions, "answers": reply.get("answers") or {}}
        if reply.get("response"):
            updated["response"] = reply["response"]
        return PermissionResultAllow(updated_input=updated)

    def waiter(self, wid, kind):
        w = self.waiters.get((wid or "").strip())
        if not w or w.kind != kind or w.future.done():
            raise HubError(f"{'审批请求' if kind == 'approval' else '问题'} {wid} 不存在或已经处理过了", status=409)
        return w

    async def decide(self, req, allow):
        w = self.waiter(req.get("req"), "approval")
        reason = (req.get("reason") or "").strip()
        via = req.get("via") or "agentctl"
        w.future.set_result((allow, "" if allow else (reason or "机主拒绝了这个操作")))
        s = self.sessions.get(w.sid)
        if s:
            self.emit(s, "approval_resolved", req=w.id, allow=allow, via=via, reason=reason)
        label = VIA.get(via, via)
        await self.tg_edit(w, f"✅ 已批准（{label}）" if allow else f"❌ 已拒绝（{label}）" + (f"：{reason}" if reason else ""))
        return {"req": w.id, "allow": allow, "tool": w.tool, "session": s.name if s else w.sid[:8]}

    async def cmd_answer(self, req):
        """回答 Claude 的提问：answers 按问题原文对应选项 label（多选给数组），option 是 Telegram 按钮的快捷方式。"""
        w = self.waiter(req.get("qid"), "question")
        via = req.get("via") or "agentctl"
        answers = req.get("answers") or {}
        if req.get("option") is not None and len(w.questions) == 1:
            opts = w.questions[0].get("options") or []
            i = int(req["option"])
            if not 0 <= i < len(opts):
                raise HubError("没有这个选项")
            answers = {w.questions[0].get("question"): opts[i].get("label")}
        skip = bool(req.get("skip"))
        response = (req.get("response") or "").strip()
        if not skip and not response:
            missing = [q.get("question") for q in w.questions if not answers.get(q.get("question"))]
            if missing:
                raise HubError("还有问题没回答：" + "；".join(missing))
        w.future.set_result({"answers": answers, "response": response, "skip": skip})
        s = self.sessions.get(w.sid)
        if s:
            self.emit(s, "question_resolved", qid=w.id, state="skipped" if skip else "answered", answers=answers,
                      response=response, via=via)
        shown = "；".join(f"{v if isinstance(v, str) else ', '.join(v)}" for v in answers.values()) or response
        await self.tg_edit(w, f"⏭ 已跳过（{VIA.get(via, via)}）" if skip else f"✅ 已回答（{VIA.get(via, via)}）：{shown}")
        return {"qid": w.id, "skipped": skip, "session": s.name if s else w.sid[:8]}

    async def notify_result(self, s, result, secs):
        head = {"idle": "✅ {} 完成", "interrupted": "⏹ {} 已中断"}.get(s.status, "⚠️ {} 出错")
        meta = [fmt_secs(secs)]
        if result is not None:
            meta.append(f"{result.num_turns} 步")
            if s.last_cost:   # 这一轮的费用（usage.jsonl 求和），不是 SDK 报的会话累计
                meta.append(f"${s.last_cost:.2f}")
        pct = float((s.context or {}).get("percent") or 0)
        if pct >= 50:
            meta.append(f"上下文 {pct:.0f}%")
        body = s.last_result if s.status == "idle" else (s.last_error + "\n\n" + s.last_result).strip()
        if pct >= 70:
            body = (body + "\n\n⚠️ 上下文快到上限了：建议先压缩或新开会话，hub 不会自动压缩").strip()
        await self.tg_send(f"{head.format('<b>' + html.escape(s.name) + '</b>')} · {' · '.join(meta)}\n\n"
                           f"{html.escape(clip(body or '（没有文字结果）', TG_TEXT_LIMIT))}\n\n"
                           f"<code>{s.id[:8]}</code>")

    # ---- Telegram（用量机器人，机主私聊）----

    async def tg_api(self, method, payload):
        if not self.tg:
            return None
        token, chat = self.tg
        try:
            r = await asyncio.to_thread(tg_call, token, method, {"chat_id": chat, **payload})
            return r.get("result")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            print(f"telegram {method} failed: HTTP {e.code} {body}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"telegram {method} failed: {type(e).__name__}", file=sys.stderr, flush=True)
        return None

    async def tg_send(self, text, buttons=None):
        payload = {"text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                                           for row in buttons]}
        r = await self.tg_api("sendMessage", payload)
        return r.get("message_id") if r else None

    async def tg_edit(self, w, verdict):
        # 不带 reply_markup 重发正文，按钮就没了，免得同一个请求被点两次
        if w.tg_message_id is not None:
            await self.tg_api("editMessageText", {"message_id": w.tg_message_id, "parse_mode": "HTML",
                                                  "text": f"{w.html}\n\n{html.escape(verdict)}",
                                                  "disable_web_page_preview": True})

    # ---- 命令（agentctl 和 HTTP 共用）----

    async def cmd_ping(self, req):
        return {"name": HUB_NAME, "platform": sys.platform, "home": HOME, "modes": list(MODES),
                "models": [{"id": m, "label": l} for m, l in MODELS], "efforts": list(EFFORTS),
                "claude": CLAUDE_CLI, "telegram": bool(self.tg), "sessions": len(self.sessions),
                "running": len(self.tasks), "waiting": len(self.waiters), "max_turns": MAX_TURNS}

    async def cmd_list(self, req):
        rows = sorted(self.sessions.values(), key=lambda s: s.updated, reverse=True)
        out = {"sessions": [self.view(s) for s in rows]}
        if req.get("all"):
            limit = int(req.get("limit") or 15)
            infos = await asyncio.to_thread(list_sessions, limit=limit + len(self.sessions))
            out["other"] = [{"id": i.session_id, "summary": clip(i.custom_title or i.summary or "", 80),
                             "cwd": i.cwd, "branch": i.git_branch, "updated": i.last_modified / 1000}
                            for i in infos if i.session_id not in self.sessions][:limit]
        return out

    def check_mode(self, mode):
        if mode not in MODES:
            raise HubError(f"不支持权限模式 {mode}，可选：{' / '.join(MODES)}")
        return mode

    async def cmd_new(self, req):
        cwd = os.path.abspath(os.path.expanduser(req.get("cwd") or "~"))
        prompt = (req.get("prompt") or "").strip()
        name = (req.get("name") or "").strip()
        mode = req.get("mode") or "default"
        dig = digest(cmd="new", cwd=cwd, prompt=prompt, name=name, mode=mode)
        replayed = self.replay(req.get("request_id"), dig)
        if replayed:
            return replayed
        if not os.path.isdir(cwd):
            raise HubError(f"目录不存在：{cwd}")
        if not prompt:
            raise HubError("缺少提示词")
        sid = str(uuid.uuid4())
        if req.get("id"):   # 网页先给空对话分好了 ID（草稿、回执都按它归属），就用这个
            try:
                sid = str(uuid.UUID(req["id"]))
            except ValueError:
                raise HubError("会话 ID 格式不对")
            if sid in self.sessions:
                raise HubError("这个会话 ID 已经在用了", status=409)
        s = Session(id=sid, name=clip(name or os.path.basename(cwd) or sid[:8], 40), cwd=cwd,
                    mode=self.check_mode(mode), title=clip(prompt.splitlines()[0] if prompt else "", 60),
                    task_tools=bool(req.get("task_tools")))
        if valid_model(req.get("model") or ""):
            s.model = req.get("model") or ""
        if req.get("effort") in EFFORTS:
            s.effort = req["effort"]
        self.sessions[sid] = s
        self.workspaces.ensure(cwd)
        self.remember(req.get("request_id"), sid, dig)
        self.start_turn(s, prompt, bool(req.get("quiet")), qid=req.get("request_id"), turn_id=req.get("_turn_id"))
        return {"session": self.view(s)}

    async def cmd_adopt(self, req):
        return {"session": self.view(await self.adopt(req.get("id")))}

    async def cmd_model(self, req):
        """切换会话的模型和思考强度：模型在这一轮里当场生效，思考强度下一轮生效。"""
        s = self.resolve(req.get("id"))
        model = (req.get("model") if req.get("model") is not None else s.model).strip()
        effort = (req.get("effort") if req.get("effort") is not None else s.effort).strip()
        if not valid_model(model):
            raise HubError(f"模型 ID 格式不对：{model}")
        if effort not in EFFORTS:
            raise HubError(f"思考强度只能是 {' / '.join(e for e in EFFORTS if e)}")
        client = self.active.get(s.id)
        if client and model != s.model:
            try:
                await client.set_model(model or None)
            except Exception as e:
                print(f"set_model failed: {e}", file=sys.stderr, flush=True)
        self.touch(s, model=model, effort=effort)
        self.emit(s, "model", model=model, effort=effort)
        return {"session": self.view(s)}

    async def cmd_mode(self, req):
        """切换会话的权限模式；这一轮正在跑就当场生效，否则下一轮生效。"""
        s = self.resolve(req.get("id"))
        mode = self.check_mode(req.get("mode") or "")
        client = self.active.get(s.id)
        live = False
        if client:
            try:
                await client.set_permission_mode(mode)
                live = True
            except Exception as e:   # 比如这一轮是旧版本 hub 起的进程：记下来，下一轮生效
                print(f"set_permission_mode failed: {e}", file=sys.stderr, flush=True)
        self.touch(s, mode=mode)
        self.emit(s, "mode", mode=mode)
        if mode == "bypassPermissions":   # 改成全部自动通过：正在等的审批也一起放行
            for w in self.waiters_of(s):
                if w.kind == "approval" and not w.future.done():
                    await self.decide({"req": w.id, "via": req.get("via") or "agentctl"}, True)
        return {"session": self.view(s), "live": live}

    async def cmd_send(self, req):
        s = await self.adopt(req.get("id"))
        prompt = (req.get("prompt") or "").strip()
        rid = req.get("request_id")
        dig = digest(cmd="send", sid=s.id, prompt=prompt, mode=req.get("mode") or "")
        replayed = self.replay(rid, dig)
        if replayed:
            return replayed
        if not prompt:
            raise HubError("缺少消息")
        if req.get("mode"):
            s.mode = self.check_mode(req["mode"])
        if req.get("task_tools") is not None:
            s.task_tools = bool(req["task_tools"])
        quiet = bool(req.get("quiet"))
        if req.get("delivery") == "steer" and s.id in self.active:
            sid_rid = rid or secrets.token_hex(8)
            if await self.steer(s, prompt, sid_rid):
                self.remember(rid, s.id, dig)
                return {"session": self.view(s), "steered": sid_rid}
        if s.id in self.tasks or s.queue:
            # 正在跑（或者停止后队列还没跑完）：排到队尾，不插队
            qid = rid or secrets.token_hex(8)
            s.queue.append({"qid": qid, "text": prompt, "quiet": quiet, "created": time.time(),
                            **({"turn_id": req["_turn_id"]} if req.get("_turn_id") else {})})
            s.queue_paused = s.queue_paused and s.id in self.tasks
            self.save()
            self.emit(s, "queued", qid=qid, text=prompt)
            self.remember(rid, s.id, dig, queued=qid)
            self.run_next(s)
            return {"session": self.view(s), "queued": qid}
        self.remember(rid, s.id, dig)
        self.start_turn(s, prompt, quiet, qid=rid, turn_id=req.get("_turn_id"))
        return {"session": self.view(s)}

    async def cmd_withdraw(self, req):
        s = self.resolve(req.get("id"))
        qid = (req.get("qid") or "").strip()
        for i, item in enumerate(s.queue):
            if item["qid"] == qid:
                del s.queue[i]
                self.save()
                self.emit(s, "withdrawn", qid=qid, text=item["text"])
                return {"session": self.view(s), "text": item["text"]}
        raise HubError("这条消息已经开始执行，或者不在队列里", status=409)

    async def cmd_wait(self, req):
        """等到这一轮结束，或者卡在审批/提问上（让调用方去问机主），或者超时。"""
        s = self.resolve(req.get("id"))
        deadline = time.monotonic() + float(req.get("timeout") or 600)
        while s.id in self.tasks and not self.waiters_of(s):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"timed_out": True, "session": self.view(s)}
            ev = self.changed.setdefault(s.id, asyncio.Event())
            try:
                await asyncio.wait_for(ev.wait(), remaining)
            except TimeoutError:
                pass
        return {"timed_out": False, "session": self.view(s, full=True)}

    async def cmd_status(self, req):
        return {"session": self.view(self.resolve(req.get("id")), full=True)}

    async def cmd_events(self, req):
        s = self.resolve(req.get("id"))
        limit = int(req.get("limit") or 300)
        rows = self.log_of(s.id).read(after=int(req.get("after") or 0))
        return {"session": self.view(s), "events": rows[-limit:], "truncated": len(rows) > limit}

    async def cmd_history(self, req):
        """agentctl history：hub 的会话看事件流，其它会话只读地看 Claude Code 自己的记录。"""
        ref = (req.get("id") or "").strip()
        limit = max(1, int(req.get("limit") or 10))
        try:
            s = self.resolve(ref)
            rows = [(e["kind"], e) for e in self.log_of(s.id).read()
                    if e["kind"] in ("user", "assistant", "tool_use", "tool_result")]
            sid, name = s.id, s.name
        except HubError:
            if len(ref) < 4:
                raise
            info = await self.find_claude_session(ref)
            rows = transcript_events(await asyncio.to_thread(get_session_messages, info.session_id,
                                                             directory=info.cwd))
            sid, name = info.session_id, clip(info.summary or "", 40)
        return {"id": sid, "name": name, "messages": [{"kind": k, **d} for k, d in rows[-limit:]]}

    async def cmd_pending(self, req):
        return {"pending": [self.waiter_view(w) for w in self.waiters.values()]}

    async def cmd_approve(self, req):
        return await self.decide(req, True)

    async def cmd_deny(self, req):
        return await self.decide(req, False)

    async def cmd_stop(self, req):
        s = self.resolve(req.get("id"))
        client = self.active.get(s.id)
        task = self.tasks.get(s.id)
        if not task or task.done():
            raise HubError(f"「{s.name}」现在没在跑", status=409)
        turn = self.turns.get(s.id)
        if turn:
            turn.user_interrupted = True
        via = req.get("via") or "agentctl"
        for w in self.waiters_of(s):
            if not w.future.done():
                w.future.set_result(None)
                self.emit(s, f"{w.kind}_resolved", **{"req" if w.kind == "approval" else "qid": w.id},
                          allow=False, state="cancelled", via=via, reason="停止")
                await self.tg_edit(w, "⏹ 已停止")
        self.touch(s, queue_paused=True)
        if client:
            await client.interrupt()
            asyncio.create_task(self.force_stop(s.id, 20, task))
        else:
            task.cancel()
            if turn is None:  # 协程还没开始，取消不会进入 run_turn 的 finally。
                self.touch(s, status="interrupted", last_error="")
                self.emit(s, "result", status="interrupted", error="", cost=None, secs=0)
        return {"session": self.view(s)}

    async def force_stop(self, sid, grace, expected_task):
        # interrupt 一般几秒内就会收到 ResultMessage；卡住的话取消任务，disconnect 会结束 Claude 进程
        await asyncio.sleep(grace)
        task = self.tasks.get(sid)
        if task is expected_task and not task.done():
            task.cancel()

    async def cmd_dirs(self, req):
        """新建会话时选工作目录：只列 home 下面的文件夹。"""
        path = os.path.realpath(os.path.expanduser(req.get("path") or "~"))
        if path != HOME and not path.startswith(HOME + os.sep):
            raise HubError("只能选 home 目录下面的文件夹")
        if not os.path.isdir(path):
            raise HubError(f"目录不存在：{path}", status=404)
        with os.scandir(path) as it:
            dirs = sorted(e.name for e in it if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")
                          and e.name not in ("__pycache__", "node_modules"))
        recent = []
        for s in sorted(self.sessions.values(), key=lambda s: s.updated, reverse=True):
            if s.cwd not in recent and (s.cwd == HOME or s.cwd.startswith(HOME + os.sep)) and os.path.isdir(s.cwd):
                recent.append(s.cwd)
        return {"path": path, "parent": os.path.dirname(path) if path != HOME else None, "dirs": dirs[:300],
                "home": HOME, "recent": recent[:10]}

    # ---- Unix socket（agentctl）----

    async def handle_unix(self, reader, writer):
        try:
            req = json.loads(await reader.readline() or b"{}")
            fn = getattr(self, f"cmd_{req.get('cmd')}", None)
            resp = {"ok": True, **await fn(req)} if fn else {"ok": False, "error": f"未知命令 {req.get('cmd')}"}
        except HubError as e:
            resp = {"ok": False, "error": str(e)}
        except Exception as e:
            traceback.print_exc()
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
            await writer.drain()
            writer.close()
        except (ConnectionError, OSError):
            pass   # agentctl 那头已经断了（比如 ssh 断线），这一轮照样在跑


# ---- HTTP（Eta）----

async def write_sse(resp, ev):
    head = f"id: {ev['seq']}\n" if "seq" in ev else ""
    await resp.write(f"{head}event: {ev['kind']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())


async def serve_sse(request, subs, backlog=()):
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    q = asyncio.Queue(maxsize=2000)
    subs.add(q)   # 先订阅再读积压，中间产生的事件靠 seq 去重，不会漏
    last = 0
    try:
        for ev in backlog:
            await write_sse(resp, ev)
            last = ev["seq"]
        await resp.write(b": ready\n\n")
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), 20)
            except TimeoutError:
                await resp.write(b": ping\n\n")   # 保活，顺便让手机那头及时发现断线
                continue
            if ev is None:
                break
            if "seq" in ev and ev["seq"] <= last:
                continue
            await write_sse(resp, ev)
            last = ev.get("seq", last)
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        subs.discard(q)
    return resp


def same_origin(request):
    site = request.headers.get("Sec-Fetch-Site")
    if site:
        return site in ("same-origin", "none")
    origin = request.headers.get("Origin")
    return origin is None or origin.split("://", 1)[-1] == request.host


# agentbox 前端不带令牌访问的几个接口：登录、心跳、自定义图标
PUBLIC_PATHS = {"/api/login", "/api/ping", "/api/icons", "/hub/login", "/hub/token"}
BUILD = str(int(time.time()))   # 静态资源 URL 里的版本号：hub 重启就换，浏览器不会用到旧缓存


COMPRESS_MIN = 16 << 10
COMPRESS_TYPES = {"application/json", "text/html", "text/plain", "text/css", "application/javascript", "text/javascript"}


def http_app(hub, token):
    def authorized(request):
        bearer = request.headers.get("Authorization", "")
        cookie = request.cookies.get(COOKIE, "")
        # agentbox 前端的 WebSocket、文件下载和预览把令牌放在 ?token= 里，只认 GET
        query = request.query.get("token", "") if request.method == "GET" else ""
        return (secrets.compare_digest(bearer, f"Bearer {token}")
                or bool(cookie) and secrets.compare_digest(cookie, token)
                or bool(query) and secrets.compare_digest(query, token))

    @web.middleware
    async def guard(request, handler):
        try:
            if request.path.startswith(("/api/", "/hub/")) and request.path not in PUBLIC_PATHS:
                if not authorized(request):
                    raise HubError("unauthorized", status=401)
                if request.method not in ("GET", "HEAD") and not secrets.compare_digest(
                        request.headers.get("Authorization", ""), f"Bearer {token}") \
                        and not same_origin(request):
                    # 只靠 cookie 的写请求必须来自本站页面（SameSite=Strict 之外再挡一层 CSRF）
                    raise HubError("跨站请求被拒绝", status=403)
                if request.headers.get("Upgrade", "").lower() == "websocket" and not same_origin(request):
                    raise HubError("跨站连接被拒绝", status=403)
            return await handler(request)
        except HubError as e:
            if request.path.startswith("/api/"):
                code = "authentication_required" if e.status == 401 else "session_not_found" if e.status == 404 else "invalid_request"
                return hub_abx.problem(e.status, code, str(e))
            return web.json_response({"ok": False, "error": str(e)}, status=e.status,
                                     dumps=lambda o: json.dumps(o, ensure_ascii=False))
        except OSError as e:   # 文件页之类直接碰磁盘的接口：把系统错误变成页面看得懂的一句话
            if not request.path.startswith("/api/"):
                raise
            status = 404 if isinstance(e, FileNotFoundError) else 403 if isinstance(e, PermissionError) else \
                507 if e.errno in (errno.ENOSPC, errno.EDQUOT) else 500
            return web.json_response({"ok": False, "error": e.strerror or str(e)}, status=status,
                                     dumps=lambda o: json.dumps(o, ensure_ascii=False))

    async def login(request):
        b = await body(request)
        if not secrets.compare_digest(str(b.get("token") or ""), token):
            raise HubError("令牌不对", status=401)
        resp = ok({"name": HUB_NAME})
        # 只走 Tailscale / 内网的 http，没法加 Secure；HttpOnly 让页面脚本读不到令牌
        resp.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="Strict", path="/")
        return resp

    async def logout(request):
        resp = ok({})
        resp.del_cookie(COOKIE, path="/")
        return resp

    async def session_token(request):
        # 旧界面登录时种过 cookie 的浏览器：新界面（agentbox 前端用 localStorage 里的令牌）靠它自动登录，不用再输一遍
        good = bool(request.cookies.get(COOKIE)) and secrets.compare_digest(request.cookies[COOKIE], token)
        return ok({"token": token if good and same_origin(request) else ""})

    async def index(request):
        # 网页端就是 agentbox 的原版前端（ABX_DIR，Apache-2.0），接口由 hub_abx 按它的格式提供
        with open(os.path.join(ABX_DIR, "index.html")) as f:
            page = f.read().replace("{{BUILD}}", BUILD)
        # 原版前端一个字不改；只在这里注入 hub 自己的一小段样式和脚本（藏起单人版用不上的入口）
        page = page.replace("</head>", f'<link rel="stylesheet" href="/web/hub/hub.css?v={BUILD}">'
                                       f'<script src="/web/hub/hub.js?v={BUILD}" defer></script></head>', 1)
        return web.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def versioned(request):
        path = os.path.realpath(os.path.join(ABX_DIR, request.match_info["tail"]))
        if not path.startswith(ABX_DIR + os.sep) or not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    async def body(request):
        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise HubError("请求体不是 JSON")
        return data if isinstance(data, dict) else {}

    def ok(data):
        return web.json_response({"ok": True, **data}, dumps=lambda o: json.dumps(o, ensure_ascii=False))

    def sid(request):
        return request.match_info["id"]

    async def info(request):
        return ok(await hub.cmd_ping({}))

    async def sessions(request):
        return ok(await hub.cmd_list({"all": request.query.get("all") == "1", "limit": request.query.get("limit")}))

    async def create(request):
        b = await body(request)
        return ok(await hub.cmd_new({k: b.get(k) for k in ("cwd", "name", "mode", "prompt", "quiet", "request_id",
                                                       "model", "effort")}))

    async def session(request):
        return ok(await hub.cmd_status({"id": sid(request)}))

    async def adopt(request):
        return ok(await hub.cmd_adopt({"id": sid(request)}))

    async def events(request):
        return ok(await hub.cmd_events({"id": sid(request), "after": request.query.get("after"),
                                        "limit": request.query.get("limit")}))

    async def send(request):
        b = await body(request)
        return ok(await hub.cmd_send({"id": sid(request), "prompt": b.get("text"), "mode": b.get("mode"),
                                      "quiet": b.get("quiet"), "request_id": b.get("request_id"),
                                      "delivery": b.get("delivery")}))

    async def withdraw(request):
        return ok(await hub.cmd_withdraw({"id": sid(request), "qid": request.match_info["qid"]}))

    def via(b):
        return b.get("via") if b.get("via") in HTTP_CLIENTS else "eta"

    async def model(request):
        b = await body(request)
        return ok(await hub.cmd_model({"id": sid(request), "model": b.get("model"), "effort": b.get("effort")}))

    async def mode(request):
        b = await body(request)
        return ok(await hub.cmd_mode({"id": sid(request), "mode": b.get("mode"), "via": via(b)}))

    async def stop(request):
        b = await body(request)
        return ok(await hub.cmd_stop({"id": sid(request), "via": via(b)}))

    async def approval(request):
        b = await body(request)
        return ok(await hub.decide({"req": request.match_info["req"], "reason": b.get("reason"), "via": via(b)},
                                   bool(b.get("allow"))))

    async def answer(request):
        b = await body(request)
        return ok(await hub.cmd_answer({"qid": request.match_info["qid"], "answers": b.get("answers"),
                                        "response": b.get("response"), "skip": b.get("skip"), "via": via(b)}))

    async def stream(request):
        s = hub.resolve(sid(request))
        after = int(request.query.get("after") or request.headers.get("Last-Event-ID") or 0)
        log = hub.log_of(s.id)
        return await serve_sse(request, log.subs, log.read(after=after))

    async def stream_all(request):
        return await serve_sse(request, hub.global_subs)

    async def dirs(request):
        return ok(await hub.cmd_dirs({"path": request.query.get("path")}))

    @web.middleware
    async def compress_large(request, handler):
        """大块文本（历史分页常有 1MB、首页 250KB）在浏览器支持时压缩：手机走 Tailscale 慢网能省下大半流量。
        只动还没发出去的普通 Response；WebSocket（自带 permessage-deflate）、SSE、静态文件不碰。"""
        resp = await handler(request)
        if (type(resp) is web.Response and not resp.prepared and resp.body is not None
                and isinstance(resp.body, (bytes, bytearray)) and len(resp.body) >= COMPRESS_MIN
                and hdrs.CONTENT_ENCODING not in resp.headers
                and (resp.content_type or "").split(";")[0] in COMPRESS_TYPES):
            resp.enable_compression()
        return resp

    app = web.Application(middlewares=[guard, compress_large], client_max_size=4 << 20)
    r = app.router
    r.add_get("/", index)
    r.add_get("/_v/{build}/{tail:.+}", versioned)
    r.add_static("/web/", WEB_DIR)
    # hub 自己的接口（Eta 原生界面、脚本用）：/hub/…；/api/… 留给 agentbox 前端（hub_abx）
    r.add_post("/hub/login", login)
    r.add_post("/hub/logout", logout)
    r.add_get("/hub/token", session_token)
    r.add_get("/hub/info", info)
    r.add_get("/hub/sessions", sessions)
    r.add_post("/hub/sessions", create)
    r.add_get("/hub/sessions/{id}", session)
    r.add_post("/hub/sessions/{id}/adopt", adopt)
    r.add_get("/hub/sessions/{id}/events", events)
    r.add_post("/hub/sessions/{id}/messages", send)
    r.add_delete("/hub/sessions/{id}/queue/{qid}", withdraw)
    r.add_post("/hub/sessions/{id}/stop", stop)
    r.add_post("/hub/sessions/{id}/mode", mode)
    r.add_post("/hub/sessions/{id}/model", model)
    r.add_get("/hub/sessions/{id}/stream", stream)
    r.add_post("/hub/approvals/{req}", approval)
    r.add_post("/hub/questions/{qid}", answer)
    r.add_get("/hub/stream", stream_all)
    r.add_get("/hub/dirs", dirs)
    api = types.SimpleNamespace(hub=hub, ok=ok, body=body, via=via, Error=HubError, token=token)
    for mod in (hub_workspaces, hub_files, hub_git, hub_skills, hub_term, hub_abx, hub_spaces, hub_optional):   # 各功能模块自己挂路由
        mod.register(app, api)
    return app


async def bind_http(runner, host):
    # Tailscale 地址开机时可能还没起来：绑不上就隔 30 秒再试，不影响本机地址和 agentctl
    while True:
        try:
            await web.TCPSite(runner, host, HTTP_PORT).start()
            print(f"http listening on {host}:{HTTP_PORT}", flush=True)
            return
        except OSError as e:
            print(f"http bind {host}:{HTTP_PORT} failed ({e.strerror}); retry in 30s", file=sys.stderr, flush=True)
            await asyncio.sleep(30)


async def main():
    for d in (STATE_DIR, EVENTS_DIR):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    lock = open(os.path.join(STATE_DIR, "hub.lock"), "w")
    if fcntl:
        try:
            # 两个 hub 共用一份状态会抢 socket、把对方跑着的会话标成中断，所以只许一个
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(f"another agent-hub is already running with {STATE_DIR}")
    # 按 usage.jsonl 重算累计费用（幂等）：老数据里的 total_cost 是按 SDK 会话累计值逐轮加出来的，
    # 被放大几十倍；这里只改数字，不删会话、不动事件和用量明细。此刻已持有 hub.lock，不会有人抢。
    try:
        changes, before, after = repair_sessions_costs(UsageLedger(USAGE_PATH))
        if changes:
            print(f"cost totals repaired from usage.jsonl: {len(changes)} sessions, "
                  f"${before:.4f} -> ${after:.4f}", flush=True)
    except Exception:   # 修不了也不能拖住启动：照旧跑，之后的轮次照常按 usage 记
        traceback.print_exc()
    hub = Hub()
    hub.load()
    hub.save()
    hub.workspaces = hub_workspaces.Workspaces(hub, STATE_DIR, HOME, HubError)
    hub.workspaces.sync_from_sessions()
    hub.terminals = hub_term.Terminals(hub, STATE_DIR, os.path.expanduser("~/.agent-hub"))
    hub.files = hub_files.Files(hub, STATE_DIR, HOME, HubError)
    hub.git = hub_git.Git(hub, STATE_DIR, HubError)
    hub.skills = hub_skills.Skills(hub, STATE_DIR, HOME, HubError)
    hub.cli_path = CLAUDE_CLI
    hub.spaces = hub_spaces.Spaces(hub, STATE_DIR, HOME, USAGE_PATH, PRICES, HubError)
    token = load_token()

    unix_server = None
    if sys.platform != "win32":   # Windows 上只开 HTTP，agentctl 留给 Linux
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)
        old_umask = os.umask(0o177)
        unix_server = await asyncio.start_unix_server(hub.handle_unix, path=SOCK_PATH)
        os.umask(old_umask)

    runner = web.AppRunner(http_app(hub, token), access_log=None)
    await runner.setup()
    binders = [asyncio.create_task(bind_http(runner, h)) for h in HTTP_HOSTS]
    print(f"agent-hub started; socket {SOCK_PATH if unix_server else '-'}; claude={CLAUDE_CLI}; "
          f"telegram={'on' if hub.tg else 'off'}", flush=True)
    for s in list(hub.sessions.values()):   # 上次没跑完的排队消息接着跑（停止后暂停的除外）
        if not s.queue_paused:
            hub.run_next(s)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass   # Windows：Ctrl+C 走 KeyboardInterrupt
    await stop.wait()
    hub.stopping = True
    if unix_server:
        unix_server.close()

    for t in binders:
        t.cancel()
    for q in [q for log in hub.logs.values() for q in log.subs] + list(hub.global_subs):
        offer(q, None)   # 让 SSE 连接先退出，runner.cleanup 才不会等它们
    tasks = list(hub.tasks.values())
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await runner.cleanup()
    hub.save()


def repair_costs_cli(argv):
    """python3 hubd.py --repair-costs [--dry-run]：服务停着时按用量明细重算累计费用。

    先抢 hub.lock：hub 在跑的时候不能动 sessions.json（它内存里那份会把改完的覆盖掉）。
    """
    dry = "--dry-run" in argv
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    lock = open(os.path.join(STATE_DIR, "hub.lock"), "w")
    if fcntl:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            print("agent-hub 正在运行：先 systemctl --user stop agent-hub 再修（下次启动时也会自动重算）",
                  file=sys.stderr)
            return 3
    ledger = UsageLedger(USAGE_PATH)
    changes, before, after = repair_sessions_costs(ledger, dry_run=dry)
    lock.close()
    print(f"usage 行 {ledger.rows}，涉及会话 {len(ledger.by_sid)}；sessions {SESSIONS_PATH}")
    print(f"合计 ${before:.4f} -> ${after:.4f}；改动 {len(changes)} 个会话"
          f"{'（dry-run，没写盘）' if dry else ''}")
    for rec in changes:
        print(f"  {rec['id'][:8]} {clip(rec['name'], 20)}: ${rec['old']:.4f} -> ${rec['new']:.4f}")
    return 0


if __name__ == "__main__":
    if "--repair-costs" in sys.argv[1:]:
        sys.exit(repair_costs_cli(sys.argv[1:]))
    asyncio.run(main())
