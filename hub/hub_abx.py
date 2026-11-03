"""
agentbox 兼容接口（2026-10-31）：网页端直接用 agentbox（devilcoolyue/agentbox，Apache-2.0）的前端，
文件在 internal/web/static，这里按 web/src/types.d.ts、docs/chat-protocol.md 提供对话相关的 /api/…，
背后接 hub 自己的会话引擎。外壳功能在 hub_files / hub_git / hub_term / hub_skills / hub_spaces；
关闭功能的降级接口在 hub_optional。

对应关系
- agentbox 的「工作空间」（它叫 Session）= hub 的工作空间（一个项目目录）。没有容器，Agent 直接跑在 lab 上。
- agentbox 的「对话线程」（Thread）= 一个 Claude Code 会话（hub 的 Session）。新对话在发第一条消息之前
  就先分好 UUID（草稿、回执都按它归属），发消息时用这个 UUID 建 Claude Code 会话。
- 对话历史 = hub 的追加事件流；接手前的 CLI 历史冻结为前缀，避免压缩打乱分页游标。
  hub 的工具审批包装成只有「批准 / 拒绝」两个选项的提问，复用它的提问卡片。
- 发消息走 agentbox 的持久回执协议（chat_protocol 1）：每条消息带浏览器生成的编号，排队、插话、撤回、
  中断都按编号查得到，断网重发不会重复执行。
"""
import asyncio
import datetime
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid

from aiohttp import web

OWNER = os.environ.get("AGENT_HUB_USER", "lab")
TIMEZONE = os.environ.get("AGENT_HUB_TZ", "Asia/Shanghai")
EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
MODEL_OPTIONS = [
    {"id": "claude-opus-5-5", "label": "Opus 5.5"},
    {"id": "claude-fable-5-1", "label": "Fable 5.1"},
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5"},
    {"id": "claude-haiku-5-5", "label": "Haiku 5.5"},
]
DEFAULT_MODEL = "claude-opus-5-5"
APPROVE, DENY = "批准", "拒绝"
ATTACH_IN_TEXT = re.compile(r"(?<![\w./-])/shared/(\.(?:images|file)/[A-Za-z0-9][A-Za-z0-9._-]{0,255})")
ATTACH_PATH = re.compile(r"^/shared/\.(images|file)/[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
RID = re.compile(r"^[a-f0-9]{32}$")
THREAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
INPUT_KEYS = {"scope", "thread_id", "text", "model", "effort", "effort_control", "attachments", "delegation",
              "suggestions", "questions", "tasks", "delivery"}
ACTIVE = ("accepted", "starting", "running")
HISTORY_PAGE = 300
RESULT_STATE = {"idle": ("completed", ""), "interrupted": ("interrupted", "operation_cancelled"),
                "error": ("failed", "turn_failed")}


def iso(ts):
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso():
    return iso(time.time())


def dumps(o):
    return json.dumps(o, ensure_ascii=False)


def js(data, status=200, headers=None):
    return web.json_response(data, status=status, dumps=dumps, headers=headers)


def problem(status, code, message):
    """agentbox 前端按 code 认错误（chat_busy、chat_request_not_found 之类），error 是给人看的一句话。"""
    operation = secrets.token_hex(16)
    action = "wait" if code in ("chat_busy", "chat_parallel_limit", "server_stopping") else (
        "sign_in" if code in ("chat_scope_changed", "authentication_required") else
        "edit_request" if code in ("invalid_request", "chat_attachments_invalid", "chat_options_invalid") else "check_result")
    status = {"chat_request_gone": 410, "chat_attachments_invalid": 409}.get(code, status)
    return js({"ok": False, "code": code, "error": message, "operation_id": operation,
               "hint": "请查询状态并核对输入后继续。", "action": action, "retryable": action == "wait"},
              status=status, headers={"Cache-Control": "no-store", "X-Agentbox-Operation-ID": operation})


class Receipts:
    """消息回执：编号 → 状态（排队中 / 执行中 / 已完成 / 已中断 / 出错 / 已撤回）。存在 abx-receipts.json。"""

    def __init__(self, path):
        self.path = path
        try:
            with open(path) as f:
                self.items: dict[str, dict] = json.load(f)
        except (FileNotFoundError, ValueError):
            self.items = {}
        changed = False
        for r in self.items.values():
            if r["state"] in ACTIVE:   # hub 重启时这一轮跟着断了，Claude 进程已经没了：说清楚是重启打断的
                r.update(state="uncertain", error_code="server_restarted", revision=r["revision"] + 1,
                         updated_at=now_iso())
                changed = True
        if changed:
            self.save()

    def save(self):
        # 正文七天后可清，编号围栏不能删；否则旧页面重试会再次执行。
        cutoff = iso(time.time() - 7 * 86400)
        for r in self.items.values():
            if r["state"] not in (*ACTIVE, "queued", "uncertain") and r["created_at"] < cutoff:
                r["request"] = None
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.items, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        fd = os.open(os.path.dirname(self.path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def public(r):
        return {k: v for k, v in r.items() if not k.startswith("_")} if r else None


class Compat:
    def __init__(self, api, token):
        self.api = api
        self.hub = api.hub
        self.token = token
        self.Error = api.Error
        self.rooms: dict[str, set] = {}       # 工作空间 id → 对话通道的发送队列
        self.turn_ids: dict[str, str] = {}    # 会话 id → 当前这一轮的 turn id（费用记到这一轮）
        self.qcache: dict[str, list] = {}     # 提问 / 审批 id → 题目（结论消息里要带上）
        self.steer_text: dict[str, str] = {}
        self.running: dict[str, str] = {}     # 会话 id → 正在执行的那条消息的回执编号
        self.history_memo: dict[str, tuple] = {}   # 会话 id → (版本, (整段历史, 费用, 每条字节数))
        self.virtual_memo: dict[str, dict] = {}    # 会话 id → {事件对象 id: (事件, 虚拟化后的条目, 字节数)}
        self.prefix_memo: dict[str, tuple] = {}    # 冻结前缀文件 → ((mtime, 大小), (cutoff, 条目))
        self.history_lock = threading.Lock()
        self.submit_lock = asyncio.Lock()
        self.dispatch_task = None
        self.started_at: dict[str, float] = {}
        state_dir = self.hub.files.state_dir
        self.receipts = Receipts(os.path.join(state_dir, "abx-receipts.json"))
        ident = f"agent-hub/{OWNER}/{state_dir}"
        self.chat_scope = hashlib.sha256((ident + "/chat").encode()).hexdigest()
        self.draft_scope = hashlib.sha256((ident + "/draft").encode()).hexdigest()
        self.hub.listeners.append(self.on_hub_event)

    def max_parallel(self):
        return self.hub.spaces.prefs().get("max_parallel_chats", 3)

    def thread_exists(self, w, tid):
        s = self.hub.sessions.get(tid)
        return bool(s and s.cwd == w["path"] or tid == w.get("pending_thread"))

    def blocked(self, wid, tid):
        return any(r["session_id"] == wid and r["thread_id"] == tid and r["state"] == "uncertain"
                   for r in self.receipts.items.values())

    def busy_count(self, w):
        return sum(s.id in self.hub.tasks for s in self.threads_of(w))

    def kick_queue(self):
        if self.hub.stopping or self.dispatch_task and not self.dispatch_task.done():
            return
        self.dispatch_task = asyncio.create_task(self.dispatch_queue())

    async def dispatch_queue(self):
        # 排队正文沿用原回执；只派发一次，仍走 hub 原来的执行器及账本。
        async with self.submit_lock:
            for r in sorted(self.receipts.items.values(), key=lambda r: r["created_at"]):
                if r["state"] != "queued" or not r.get("_web_queue"):
                    continue
                w = self.hub.workspaces.items.get(r["session_id"])
                s = self.hub.sessions.get(r["thread_id"])
                if not w or w.get("stopped") or s and (s.id in self.hub.tasks or s.queue_paused):
                    continue
                if self.blocked(w["id"], r["thread_id"]) or self.busy_count(w) >= self.max_parallel():
                    continue
                if not self.thread_exists(w, r["thread_id"]):
                    self.receipt_set(r["request_id"], state="deleted", request=None, error_code="thread_deleted")
                    continue
                self.receipt_set(r["request_id"], state="accepted", turn_id=r.get("turn_id") or secrets.token_hex(16))
                try:
                    await self.send_message(w, r["request"], request_id=r["request_id"], thread=r["thread_id"])
                except Exception:
                    # 可能已交给执行器，保留编号并要求核对，不能删收据再重发。
                    self.receipt_set(r["request_id"], state="uncertain", error_code="execution_incomplete")

    def delete_receipts(self, wid, tid=None):
        for r in list(self.receipts.items.values()):
            if r["session_id"] == wid and (tid is None or r["thread_id"] == tid):
                self.receipt_set(r["request_id"], state="deleted", request=None,
                                 error_code="thread_deleted" if tid else "session_deleted")

    def recover_orphan(self, r):
        if r["state"] in ACTIVE and r["thread_id"] not in self.hub.tasks:
            self.receipt_set(r["request_id"], state="uncertain", error_code="execution_incomplete")
        return r

    # ---- 视图 ----

    def model_options(self):
        return [{**m, "reasoning": {"support": "supported", "control": "effort", "levels": EFFORT_LEVELS}}
                for m in MODEL_OPTIONS]

    def ws(self, sid):
        w = self.hub.workspaces.items.get(sid)
        if not w:
            raise self.Error("工作空间不存在", status=404)
        return w

    # ---- 登录、我 ----

    async def login(self, request):
        b = await self.api.body(request)
        if not b.get("password") or b.get("password") != self.token:
            return js({"error": "账号或密码错误（密码填 agentctl token 打印的令牌）"}, status=401)
        return js({"token": self.token})

    async def logout(self, request):
        response = js({})
        response.del_cookie("agent_hub", path="/")
        return response

    async def ping(self, request):
        return web.Response(text="pong")

    async def icons(self, request):
        return js({})

    async def activity(self, request):
        return web.Response(status=204)

    async def me(self, request):
        return js({
            "user": OWNER, "role": "admin", "models": {"claude": self.model_options()},
            "timezone": self.hub.spaces.settings()["timezone"], "quota": None,
            "chat_history_timeout_sec": self.hub.spaces.settings()["chat_history_timeout_sec"],
            "draft_scope": self.draft_scope, "draft_protocol": 1, "chat_protocol": 1, "chat_scope": self.chat_scope,
            "delegation_protocol": 0, "prompt_suggestions": 0, "chat_questions": 1, "chat_followups": 1,
            "chat_tasks": 1, "terminal_tips": self.hub.spaces.settings()["terminal_tips"],
        })

    # ---- 对话辅助：任务列表、大纲、/ 命令、模型 ----

    def thread_session(self, request):
        w = self.ws(request.match_info["id"])
        tid = request.query.get("thread") or self.active(w)
        if not self.thread_exists(w, tid):
            raise self.Error("对话不存在", status=404)
        return w, self.hub.sessions.get(tid)

    async def tasks(self, request):
        _, s = self.thread_session(request)
        view = await asyncio.to_thread(self.read_tasks, s) if s else {"tasks": []}
        return js(view, headers={"Cache-Control": "no-store"})

    def read_tasks(self, s):
        """输入框上方的任务面板。Claude 5 的任务清单在 ~/.claude/tasks/<会话>/<n>.json（照 agentbox chat_tasks.go：
        某一轮开始前已经全部完成的那批任务不再显示）；老模型用 TodoWrite，取最近一次。"""
        d = os.path.join(os.path.expanduser("~"), ".claude", "tasks", s.id)
        tasks = []
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        for n in names:
            m = re.fullmatch(r"([0-9]{1,9})\.json", n)
            if not m:
                continue
            p = os.path.join(d, n)
            try:
                st = os.stat(p)
                with open(p) as f:
                    t = json.load(f)
            except (OSError, ValueError):
                continue
            if str(t.get("id")) != m.group(1) or not t.get("subject") or (t.get("metadata") or {}).get("_internal"):
                continue
            created = getattr(st, "st_birthtime", None) or st.st_mtime
            tasks.append((int(m.group(1)), t, created, st.st_mtime))
        if tasks:
            tasks.sort(key=lambda x: x[0])
            starts = [e["ts"] for e in self.hub.log_of(s.id).read() if e["kind"] == "user"]
            done = [False] * len(tasks)
            for start in starts:   # 这一轮开始前已经建好而且全部完成的那批，算上一批计划，不再显示
                before = [i for i, x in enumerate(tasks) if not done[i] and x[2] < start]
                if before and all(tasks[i][1].get("status") == "completed" and tasks[i][3] < start for i in before):
                    for i, x in enumerate(tasks):
                        if x[2] < start:
                            done[i] = True
            shown = [x[1] for i, x in enumerate(tasks) if not done[i]][:200]
            open_ids = {str(t["id"]) for t in shown if t.get("status") != "completed"}
            return {"tasks": [{"id": str(t["id"]), "subject": t["subject"], "active_form": t.get("activeForm") or "",
                               "status": t.get("status") or "pending",
                               **({"blocked_by": [b for b in t.get("blockedBy") or [] if str(b) in open_ids]}
                                  if t.get("blockedBy") else {})} for t in shown]}
        todos = latest_todos(s)
        return {"tasks": [{"id": str(i + 1), "subject": t.get("content") or "", "active_form": t.get("activeForm") or "",
                           "status": t.get("status") or "pending"} for i, t in enumerate(todos)]}

    async def outline(self, request):
        _, s = self.thread_session(request)
        if not s:
            return js({"thread_id": request.query.get("thread") or "", "items": []})
        entries, _, _ = await asyncio.to_thread(self.history_cached, s)
        items, last = [], None
        for i, e in enumerate(entries):
            if e["kind"] == "user":
                last = {"line": i, "ts": e["ts"], "text": e["text"][:300]}
                items.append(last)
            elif last and e["kind"] == "event" and e["event"].get("type") == "assistant":
                text = "".join(b.get("text", "") for b in (e["event"].get("message") or {}).get("content") or []
                               if isinstance(b, dict) and b.get("type") == "text").strip()
                if text:
                    last["answer"] = text[:300]
        return js({"thread_id": s.id, "items": items})

    async def commands(self, request):
        _, s = self.thread_session(request)
        info = (s.command_info if s else []) or [{"name": n} for n in (s.commands if s else [])]
        if not info:   # 新对话还没跑过：给几条常用的，跑过一轮后换成 Claude Code 报的完整列表
            info = [{"name": n} for n in ("compact", "context", "init", "review", "cost")]
        return js({"state": "available", "commands": [
            {"name": c["name"], "description": c.get("description") or "", "argument_hint": c.get("argument_hint") or "",
             "aliases": [], "builtin": True} for c in info]})

    async def models(self, request):
        w = self.ws(request.match_info["id"])
        return js({"models": self.model_options(), "discovery": "available", "default_model": w.get("default_model") or DEFAULT_MODEL,
                   "default_reasoning": {"support": "supported", "control": "effort", "levels": EFFORT_LEVELS},
                   "client_effort": ""})

    # ---- 对话线程 = Claude Code 会话 ----

    def threads_of(self, w):
        return sorted((s for s in self.hub.sessions.values() if s.cwd == w["path"]),
                      key=lambda s: s.updated, reverse=True)

    def pending_thread(self, w, fresh=False):
        """还没发过消息的新对话：先分好 UUID。已经有一个空的就复用，不堆空对话。"""
        pid = w.get("pending_thread")
        if fresh or not pid or pid in self.hub.sessions:
            pid = str(uuid.uuid4())
            w["pending_thread"] = pid
            self.hub.workspaces.save()
        return pid

    def active(self, w):
        tid = w.get("active_thread")
        if tid and self.thread_exists(w, tid):
            return tid
        threads = self.threads_of(w)
        if threads:
            return threads[0].id
        tid = self.pending_thread(w)
        w["active_thread"] = tid
        self.hub.workspaces.save()
        return tid

    def set_active(self, w, tid):
        w["active_thread"] = tid
        self.hub.workspaces.save()
        self.broadcast(w["id"], {"type": "thread", "id": tid})

    def thread(self, s, tid=None):
        if s is None:
            t = now_iso()
            return {"id": tid, "title": "新对话", "ts": t, "updated": t, "turns": 0, "resumable": False}
        path = transcript_path(s)
        return {"id": s.id, "title": s.title or s.name, "ts": iso(s.created), "updated": iso(s.updated),
                "turns": s.turns, "resumable": s.started,
                **({"size": os.path.getsize(path)} if path and os.path.exists(path) else {})}

    async def threads(self, request):
        w = self.ws(request.match_info["id"])
        active = self.active(w)
        items = [self.thread(s) for s in self.threads_of(w)]
        return js({"threads": items, "active": active})

    async def thread_new(self, request):
        w = self.ws(request.match_info["id"])
        current = self.active(w)
        if current not in self.hub.sessions:   # 当前就是空对话：复用
            return js({"id": current, "created": False})
        tid = self.pending_thread(w)
        self.set_active(w, tid)
        return js({"id": tid, "created": True})

    async def thread_activate(self, request):
        w = self.ws(request.match_info["id"])
        tid = request.match_info["tid"]
        if not self.thread_exists(w, tid):
            raise self.Error("对话不存在", status=404)
        self.set_active(w, tid)
        s = self.hub.sessions.get(tid)
        return js({"id": tid, "resumable": bool(s and s.started)})

    async def thread_patch(self, request):
        w = self.ws(request.match_info["id"])
        s = self.hub.sessions.get(request.match_info["tid"])
        if not s or not self.thread_exists(w, s.id):
            raise self.Error("对话不存在", status=404)
        b = await self.api.body(request)
        title = (b.get("title") or "").strip()[:80]
        if not title:
            raise self.Error("标题不能是空的")
        self.hub.touch(s, title=title)
        self.broadcast(self.ws_of(s)["id"], {"type": "thread_title", "id": s.id, "title": title})
        return js({"id": s.id, "title": title})

    async def thread_delete(self, request):
        w = self.ws(request.match_info["id"])
        tid = request.match_info["tid"]
        if not self.thread_exists(w, tid):
            raise self.Error("对话不存在", status=404)
        was_active = self.active(w) == tid
        s = self.hub.sessions.get(tid)
        if s:
            if s.id in self.hub.tasks:
                raise self.Error("这个对话还在跑，先停下来", status=409)
            self.hub.sessions.pop(s.id)   # 只从 hub 移除，Claude Code 自己的会话记录还在
            self.hub.save()
        self.delete_receipts(w["id"], tid)
        self.history_memo.pop(tid, None)
        if w.get("pending_thread") == tid:
            w.pop("pending_thread", None)
            self.hub.workspaces.save()
        if was_active:
            rest = self.threads_of(w)
            self.set_active(w, rest[0].id if rest else self.pending_thread(w, fresh=True))
        self.broadcast(w["id"], {"type": "thread_deleted", "id": tid})
        return js({})

    def ws_of(self, s):
        return self.hub.workspaces.ensure(s.cwd)

    # ---- 路径：页面上用 agentbox 的 /workspace、/shared，发给 Claude 的是 lab 上的真实路径 ----

    def virtualize(self, text, ws_path):
        """发往页面的 JSON 文本：共享目录换成 /shared（出现在哪都换，聊天附件的缩略图要靠它），
        工作空间目录只换整段就是路径的字符串（工具的 file_path、Glob 结果之类），命令里夹着的路径不动。
        这样工具行显示相对路径，点路径能直接打开文件，跟 agentbox 一样。"""
        text = text.replace(self.hub.files.shared + "/", "/shared/")
        ws = ws_path.rstrip("/")
        if ws:
            text = text.replace('"' + ws + '/', '"/workspace/').replace('"' + ws + '"', '"/workspace"')
        return text

    def to_host(self, text):
        """消息里的附件路径（/shared/.images/…）换成真实路径，Claude 才读得到。"""
        return ATTACH_IN_TEXT.sub(lambda m: self.hub.files.shared + "/" + m.group(1), text)

    # ---- 历史：Claude Code 的会话文件为主干，穿插 hub 才有的耗时、费用、提问、审批、插话 ----

    async def history(self, request):
        """游标按追加式事件序列计数；支持向前窗口和重连 from，不能把它们当最新页。"""
        w = self.ws(request.match_info["id"])
        q = request.query
        older = "before" in q or "after" in q
        tid = q.get("thread") or self.active(w)
        if not self.thread_exists(w, tid):
            if older:
                return problem(400, "invalid_request", "历史游标所属的对话不存在")
            tid = self.active(w)
        if older and not q.get("thread"):
            return problem(400, "invalid_request", "历史分页需要对话编号")
        s = self.hub.sessions.get(tid)
        full, costs, sizes = await asyncio.to_thread(self.history_cached, s) if s else ([], {}, [])
        n = len(full)

        def index(key, lo, hi):
            value = int(q[key])
            if not lo <= value <= hi:
                raise ValueError
            return value

        def page_end(start, until):
            size = count = 0
            end = start
            while end < until:
                if end > start and (count >= 600 or size + sizes[end] > 4 << 20):
                    break
                if end > start and full[end]["kind"] == "user" and (count >= HISTORY_PAGE or size >= 1 << 20):
                    break
                size += sizes[end]
                count += 1
                end += 1
            return end

        try:
            if "after" in q:
                start = index("after", 0, n - 1)
                until = index("until", start + 1, n) if "until" in q else n
                end = page_end(start, until)
            else:
                end = index("before", 1, n) if "before" in q else n
                start = end
                size = 0
                while start > 0:
                    candidate = start - 1
                    if start < end and (end - start >= 600 or size + sizes[candidate] > 4 << 20):
                        break
                    start = candidate
                    size += sizes[start]
                    if full[start]["kind"] == "user" and (end - start >= HISTORY_PAGE or size >= 1 << 20):
                        break
                if not older and "from" in q and q.get("thread") == tid:
                    try:
                        wanted = index("from", 0, n)
                        if n - wanted <= 12000 and sum(sizes[wanted:]) <= 64 << 20:
                            start = wanted
                    except ValueError:
                        pass  # 旧游标失效时上游退回最新页。
        except ValueError:
            return problem(400, "invalid_request", "历史游标无效")
        entries = full[start:end]
        if start < end and start > 0 and full[start]["kind"] != "user":
            user = next((e for e in reversed(full[:start]) if e["kind"] == "user"), None)
            if user and user.get("turn"):
                entries = [{"kind": "turn_context", "ts": user["ts"], "turn": user["turn"]}, *entries]
        turn_ids = {e["turn"]["id"] for e in entries if e.get("turn")}
        out = {"entries": entries, "costs": {k: v for k, v in costs.items() if k in turn_ids},
               "start": start, "has_more": start > 0}
        if older:
            out["thread_id"] = tid
        else:
            # active_thread 是本响应的线程，另一页面的 active 指针不能改写本页草稿归属。
            out.update(active_thread=tid, thread=self.thread(s, tid))
        if "after" in q:
            out.update(end=end, has_newer=end < n)
        return js(out, headers={"Cache-Control": "no-store"})

    def history_cached(self, s):
        """整段历史按（会话文件大小 + 修改时间 + hub 事件序号）缓存，长对话翻页时不用每页都重读几十 MB。"""
        with self.history_lock:
            return self._history_cached(s)

    def _history_cached(self, s):
        path = transcript_path(s)
        try:
            st = os.stat(path) if path else None
        except OSError:
            st = None
        key = (st.st_size, st.st_mtime_ns) if st else None, self.hub.log_of(s.id).seq
        hit = self.history_memo.get(s.id)
        if hit and hit[0] == key:
            return hit[1]
        value = self.build_history(s)
        self.history_memo.pop(s.id, None)   # 重新插到末尾：淘汰的是最久没看的会话
        self.history_memo[s.id] = (key, value)
        while len(self.history_memo) > 3:
            old = next(iter(self.history_memo))
            self.history_memo.pop(old)
            self.virtual_memo.pop(old, None)
        return value

    def imported_history(self, s, cutoff):
        """hub 原样存 Claude 事件（agent_event）之前的那段对话：从 CLI 转录读完整内容，冻结成文件。

        冻结后不再随 resume / 压缩变化，网页拿到的历史游标保持稳定。cutoff 是这一段的结束时间
        （第一个原样事件的时间；会话从来没有原样事件时就是冻结那一刻），一起存进文件。
        """
        path = self.hub.log_of(s.id).path + ".history2.json"
        try:
            st = os.stat(path)
            hit = self.prefix_memo.get(path)
            if hit and hit[0] == (st.st_mtime_ns, st.st_size):
                return hit[1]
            with open(path) as f:
                data = json.load(f)
            value = data["cutoff"], data["entries"]
            self.prefix_memo.pop(path, None)
            self.prefix_memo[path] = ((st.st_mtime_ns, st.st_size), value)
            while len(self.prefix_memo) > 3:
                self.prefix_memo.pop(next(iter(self.prefix_memo)))
            return value
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            pass
        cutoff = cutoff or now_iso()
        entries = []
        for e in read_transcript_full(s):
            ts = e.get("timestamp") or ""
            if ts >= cutoff:
                continue
            msg = e.get("message") or {}
            if e.get("type") == "user" and not is_tool_result(msg):
                if (e.get("origin") or {}).get("kind", "human") == "human" and user_text(msg):
                    entries.append({"kind": "user", "text": user_text(msg), "ts": ts})
            elif e.get("type") in ("assistant", "user"):
                ev = {"type": e["type"], "message": msg, "parent_tool_use_id": None, "uuid": e.get("uuid")}
                if e.get("toolUseResult") is not None:
                    ev["tool_use_result"] = e["toolUseResult"]
                # 旧段工具输出截得比实时短：一个月的终端会话全文能有上百 MB，翻页和内存都吃不消
                entries.append({"kind": "event", "event": slim_old(self.hub.slim_event(ev, 2000)), "ts": ts})
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".history-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"cutoff": cutoff, "entries": entries}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return cutoff, entries

    def cost_view(self, s, tid, cost):
        source = "table" if self.hub.ledger.turn_cost(s.id, tid) is not None else "provider"
        return {"turn_id": tid, "cost_micro_usd": round((cost or 0) * 1e6), "source": source}

    def build_history(self, s):
        events = self.hub.log_of(s.id).read()
        entries, costs, questions = [], {}, {}
        turn_id = None
        result_entries = []
        # 10-27 下午之前 hub 只存自己简化过的事件（工具只有摘要），那一段以 CLI 转录为准（冻结，跨压缩读全）；
        # 之后 hub 原样存 Claude 事件，以 hub 事件流为准：CLI 转录可能落后、压缩或分叉，不能拿它重排游标。
        raw_start = next((iso(e["ts"]) for e in events if e["kind"] == "agent_event"), None)
        legacy = [e for e in events if raw_start is None or iso(e["ts"]) < raw_start]
        cutoff = None
        if s.adopted or any(e["kind"] in ("assistant", "tool_use") for e in legacy):
            cutoff, prefix = self.imported_history(s, raw_start)
            # 前缀条目是缓存共享的；下面会给用户消息补 turn，所以这些要复制
            entries.extend(dict(x) if x["kind"] == "user" else x for x in prefix)
        merged = []   # 旧段里 hub 才有的条目（提问、审批、插话、每轮结束），按时间插进转录前缀
        for e in events:
            k, ts = e["kind"], iso(e["ts"])
            old = cutoff is not None and ts < cutoff
            if old and k not in ("user", "result", "question", "question_resolved", "approval",
                                 "approval_resolved", "steer"):
                continue   # 旧段的回复、思考、工具以转录为准
            out = merged if old else entries
            if k == "user":
                result_entries = []
                turn_id = (e.get("turn") or {}).get("id")
                if old:
                    match = next((x for x in entries if x["kind"] == "user" and "turn" not in x
                                  and x.get("text", "").strip() == e.get("text", "").strip()), None)
                    if match is not None:
                        match["turn"] = e.get("turn")
                    elif not e.get("imported"):
                        out.append({"kind": "user", "text": e.get("text", ""), "ts": ts, "turn": e.get("turn")})
                else:
                    out.append({"kind": "user", "text": e.get("text", ""), "ts": ts, "turn": e.get("turn")})
            elif k == "agent_event":
                ev = e["event"]
                if ev.get("type") != "stream_event" and not ev.get("isReplay"):
                    out.append({"kind": "event", "event": ev, "ts": ts,
                                **({"state": e["state"]} if e.get("state") else {})})
                    if ev.get("type") == "result":
                        result_entries.append(len(out) - 1)
            elif k == "result":
                if e.get("status") == "interrupted" and not old:
                    for index in result_entries:
                        out[index]["state"] = "interrupted"
                if turn_id and e.get("cost") is not None:
                    costs[turn_id] = self.cost_view(s, turn_id, e["cost"])
                out.append({"kind": "status", "state": e.get("status") or "idle", "error": e.get("error") or "",
                            "duration_ms": round((e.get("secs") or 0) * 1000), "ts": ts})
            elif k in ("question", "question_resolved", "approval", "approval_resolved"):
                view = self.question_view(e, questions)
                if view:
                    out.append({"kind": "question", "event": view, "ts": ts})
            elif k == "steer":
                view = self.steer_view(e)
                if view["state"] in ("applied", "dropped"):
                    out.append({"kind": "steer", "event": view, "ts": ts})
        if merged:   # 前缀和旧段 hub 条目都只在 cutoff 之前，按时间稳定合并，结果不随后续追加变化
            head = [x for x in entries if x.get("ts", "") < cutoff]
            tail = entries[len(head):]
            entries = sorted(head + merged, key=lambda x: x.get("ts") or "") + tail
        entries, sizes = self.virtual_entries(s, entries)
        return entries, costs, sizes

    def virtual_entries(self, s, entries):
        """路径虚拟化逐条做，Claude 事件按对象缓存（事件行本身由 EventLog 缓存，跨重建是同一个对象）：
        会话进行中每来一条事件都要重建历史，不能每次把几十 MB 整段序列化、替换、再解析。
        顺带记下每条的字节数，翻页按字节截断时不用再序列化一遍。"""
        old = self.virtual_memo.get(s.id) or {}
        memo, out, sizes = {}, [], []
        for e in entries:
            ev = e.get("event") if e["kind"] == "event" else None
            key = (id(ev), e.get("state"), e.get("ts")) if ev is not None else None
            hit = old.get(key) if key else None
            if hit is None or hit[0] is not ev:
                text = self.virtualize(dumps(e), s.cwd)
                hit = (ev, json.loads(text), len(text.encode()))
            if key:
                memo[key] = hit
            out.append(hit[1])
            sizes.append(hit[2])
        self.virtual_memo[s.id] = memo
        return out, sizes

    # ---- hub 事件 → agentbox 的对话消息、回执状态 ----

    def question_view(self, e, cache):
        """提问照搬；工具审批包装成只有「批准 / 拒绝」两个选项的提问，复用 agentbox 的提问卡片。"""
        k = e["kind"]
        if k == "question":
            qs = [{"question": q.get("question"), "header": q.get("header") or "",
                   "options": [{"label": o.get("label"), "description": o.get("description") or ""}
                               for o in q.get("options") or []],
                   "multi_select": bool(q.get("multiSelect"))} for q in e.get("questions") or []]
            cache[e["qid"]] = qs
            return {"id": e["qid"], "state": "pending", "questions": qs,
                    "expires_at": iso(e["ts"] + self.hub_timeout("question"))}
        if k == "question_resolved":
            qs = cache.get(e["qid"]) or []
            answers = {q: (", ".join(v) if isinstance(v, list) else v) for q, v in (e.get("answers") or {}).items()}
            return {"id": e["qid"], "state": e.get("state") or "answered", "questions": qs, "answers": answers}
        if k == "approval":
            qs = [{"question": approval_question(e), "header": "权限",
                   "options": [{"label": APPROVE, "description": "执行这一步"},
                               {"label": DENY, "description": "不执行，让 Claude 换个办法"}],
                   "multi_select": False}]
            cache["ap-" + e["req"]] = qs
            return {"id": "ap-" + e["req"], "state": "pending", "questions": qs,
                    "expires_at": iso(e["ts"] + self.hub_timeout("approval"))}
        if k == "approval_resolved":
            qid = "ap-" + e["req"]
            qs = cache.get(qid) or []
            state = {"expired": "expired", "cancelled": "cancelled"}.get(e.get("state"), "answered")
            answers = {qs[0]["question"]: APPROVE if e.get("allow") else DENY} if qs and state == "answered" else {}
            return {"id": qid, "state": state, "questions": qs, "answers": answers}
        return None

    def hub_timeout(self, kind):
        return int(os.environ.get("AGENT_HUB_APPROVAL_TIMEOUT" if kind == "approval" else "AGENT_HUB_QUESTION_TIMEOUT",
                                  "1800"))

    def steer_view(self, e):
        text = e.get("text")
        if text:
            self.steer_text[e["qid"]] = text
        state = {"sent": "pending", "queued": "pending", "applied": "applied", "dropped": "dropped"}.get(e.get("state"),
                                                                                                       e.get("state"))
        return {"id": e["qid"], "text": text or self.steer_text.get(e["qid"], ""), "state": state}

    def receipt_set(self, rid, **changes):
        r = self.receipts.items.get(rid)
        if not r:
            return
        r.update(changes)
        r["revision"] += 1
        r["updated_at"] = now_iso()
        self.receipts.save()
        self.broadcast(r["session_id"], {"type": "chat_request", "version": 1, "receipt": Receipts.public(r)})

    def track_receipts(self, s, ev):
        k = ev["kind"]
        qid = ev.get("qid")
        if k == "user":
            if qid in self.receipts.items:
                self.running[s.id] = qid
                self.receipt_set(qid, state="running", turn_id=(ev.get("turn") or {}).get("id") or "")
            else:
                self.running.pop(s.id, None)
        elif k == "queued" and qid in self.receipts.items:
            self.receipt_set(qid, state="queued")
        elif k == "dequeued" and qid in self.receipts.items:
            self.receipt_set(qid, state="accepted")
        elif k == "withdrawn" and qid in self.receipts.items:
            self.receipt_set(qid, state="abandoned", error_code="withdrawn")
        elif k == "steer" and ev.get("state") == "sent" and qid in self.receipts.items:
            self.receipt_set(qid, state="completed", turn_id="", merged_into=self.running.get(s.id) or "0" * 32)
        elif k == "result":
            rid = self.running.pop(s.id, None)
            if rid:
                state, code = RESULT_STATE.get(ev.get("status") or "idle", ("failed", "turn_failed"))
                if ev.get("error_code"):
                    state, code = "uncertain", ev["error_code"]
                self.receipt_set(rid, state=state, **({"error_code": code} if code else {}))

    def on_hub_event(self, s, ev):
        if ev["kind"] in ("user", "queued", "dequeued", "withdrawn", "steer", "result"):
            self.track_receipts(s, ev)
        if ev["kind"] == "user":
            self.turn_ids[s.id] = (ev.get("turn") or {}).get("id")
            self.started_at[s.id] = ev["ts"]
        w = self.hub.workspaces.by_path(s.cwd)
        if not w:
            return
        k, ts = ev["kind"], iso(ev["ts"])
        out = []
        if k == "user":
            out.append({"type": "user_message", "text": ev.get("text", ""), "ts": ts, "turn": ev.get("turn")})
        elif k == "agent_event":
            event = ev["event"]
            if not (event.get("type") == "stream_event" and event.get("parent_tool_use_id")):
                out.append({"type": "agent_event", "event": event, "ts": ts,
                            **({"state": ev["state"]} if ev.get("state") else {})})
        elif k == "status":
            st = ev.get("status")
            if st in ("running", "waiting"):
                if st == "running":
                    out.append({"type": "status", "state": "running", "ts": ts, "phase": ""})
            # 最终 status 要在费用和回执收尾之后；touch 的临时状态不能抢先结束 UI。
        elif k == "usage":
            tid = self.turn_ids.get(s.id)
            cost = self.hub.ledger.turn_cost(s.id, tid) if tid else None
            if cost is not None:
                out.append({"type": "turn_cost", "running": True, "cost": self.cost_view(s, tid, cost)})
        elif k == "result":
            tid = self.turn_ids.get(s.id)
            if tid and ev.get("cost") is not None:
                out.append({"type": "turn_cost", "running": False,
                            "cost": self.cost_view(s, tid, ev["cost"])})
            out.append({"type": "status", "state": ev.get("status") or "idle", "error": ev.get("error") or "",
                        "duration_ms": round((ev.get("secs") or 0) * 1000), "ts": ts})
            # task 的 done callback 才释放执行槽；下一轮调度延到那之后。
            asyncio.get_running_loop().call_soon(self.kick_queue)
        elif k in ("question", "question_resolved", "approval", "approval_resolved"):
            view = self.question_view(ev, self.qcache)
            if view:
                out.append({"type": "question", "event": view, "ts": ts})
        elif k == "steer":
            view = self.steer_view(ev)
            if view["state"] in ("applied", "dropped"):
                out.append({"type": "steer", "event": view, "ts": ts})
        for msg in out:
            self.broadcast(w["id"], {"thread": s.id, **msg})
        if ev["kind"] == "user":
            self.broadcast(w["id"], {"type": "thread_title", "id": s.id, "title": s.title or s.name})

    def broadcast(self, wid, msg):
        for q in list(self.rooms.get(wid, ())):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # 丢掉增量会留下破损工具行；断开让前端走历史补拉。
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)

    async def chat_ws(self, request):
        w = self.ws(request.match_info["id"])
        wsr = web.WebSocketResponse(heartbeat=30, max_msg_size=4 << 20)
        await wsr.prepare(request)
        q = asyncio.Queue(maxsize=5000)
        self.rooms.setdefault(w["id"], set()).add(q)

        async def writer():
            while True:
                msg = await q.get()
                if msg is None:
                    await wsr.close(code=1013, message=b"reload history")
                    return
                await wsr.send_str(self.virtualize(dumps(msg), w["path"]))

        wtask = asyncio.create_task(writer())
        try:
            threads = {s.id: {"phase": "", "elapsed_ms": max(0, round((time.time() -
                         self.started_at.get(s.id, getattr(self.hub.turns.get(s.id), "sent_at", s.updated))) * 1000))}
                       for s in self.threads_of(w) if s.id in self.hub.tasks}
            status = {"type": "status", "state": "idle", "threads": threads, "max_parallel": self.max_parallel()}
            if self.active(w) in threads:
                status.update(state="running", **threads[self.active(w)])
            await wsr.send_str(dumps(status))
            async for msg in wsr:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                try:
                    if data.get("type") == "user_message":   # 旧发送路径（没有编号）
                        async with self.submit_lock:
                            tid = data.get("thread") or self.active(w)
                            if not isinstance(tid, str) or not self.thread_exists(w, tid):
                                raise self.Error("对话不存在", status=404)
                            if self.blocked(w["id"], tid) or any(r["session_id"] == w["id"] and
                                    r["thread_id"] == tid and r["state"] == "queued" for r in self.receipts.items.values()):
                                raise self.Error("请先核对未结束的消息", status=409)
                            if tid in self.hub.tasks or self.busy_count(w) >= self.max_parallel():
                                raise self.Error("对话仍在运行或同时运行数已达上限", status=409)
                            await self.send_message(w, data, thread=tid)
                    elif data.get("type") == "interrupt":
                        tid = data.get("thread") or self.active(w)
                        s = self.hub.sessions.get(tid) if self.thread_exists(w, tid) else None
                        if s and s.id in self.hub.tasks:
                            await self.hub.cmd_stop({"id": s.id, "via": "web"})
                except self.Error as e:
                    await wsr.send_str(dumps({"type": "error", "error": str(e)}))
        finally:
            self.rooms.get(w["id"], set()).discard(q)
            wtask.cancel()
            await asyncio.gather(wtask, return_exceptions=True)
        return wsr

    async def send_message(self, w, data, request_id=None, delivery=None, thread=None):
        text = self.to_host((data.get("text") or "").strip())
        if not text:
            return None
        model = data.get("model") or w.get("default_model") or DEFAULT_MODEL
        effort = data.get("effort") or ""
        tasks = bool(data.get("tasks")) if "tasks" in data else None
        sid = thread or self.active(w)
        receipt = self.receipts.items.get(request_id) or {}
        turn_id = receipt.get("turn_id") or None
        self.hub.spaces.start(w)
        if sid not in self.hub.sessions:   # 空对话的第一条：用先分好的 UUID 建 Claude Code 会话
            r = await self.hub.cmd_new({"id": sid, "cwd": w["path"], "prompt": text, "model": model or "",
                                        "effort": effort or "", "mode": w.get("mode") or "bypassPermissions",
                                        "request_id": request_id, "task_tools": tasks, "_turn_id": turn_id})
            if w.get("pending_thread") == sid:
                w.pop("pending_thread", None)
                self.hub.workspaces.save()
            return r
        s = self.hub.sessions[sid]
        if s.id not in self.hub.tasks and (model != s.model or effort != s.effort):
            await self.hub.cmd_model({"id": sid, "model": model if model is not None else s.model,
                                      "effort": effort if effort is not None else s.effort})
        return await self.hub.cmd_send({"id": sid, "prompt": text, "request_id": request_id, "delivery": delivery,
                                        "task_tools": tasks, "_turn_id": turn_id})

    # ---- 消息回执（chat_protocol 1）----

    async def request_put(self, request):
        async with self.submit_lock:
            return await self.accept_request(request)

    async def accept_request(self, request):
        w = self.ws(request.match_info["id"])
        rid = request.match_info["request"]
        if not RID.fullmatch(rid):
            return problem(400, "invalid_request", "消息编号不对")
        try:
            b = await request.json()
        except ValueError:
            return problem(400, "invalid_request", "请求体不是 JSON")
        if not isinstance(b, dict) or set(b) - INPUT_KEYS:
            return problem(400, "invalid_request", "请求里有不认识的字段，请刷新页面")
        for key in ("scope", "thread_id", "text", "model", "effort", "effort_control"):
            limit = 1 << 20 if key == "text" else 80 if key == "thread_id" else 512
            if not isinstance(b.get(key), str) or len(b[key].encode()) > limit:
                return problem(400, "invalid_request", "消息字段不对或太长")
        if not THREAD_ID.fullmatch(b["thread_id"]):
            return problem(400, "invalid_request", "对话编号无效")
        if any(key in b and not isinstance(b[key], bool) for key in ("questions", "suggestions", "tasks")):
            return problem(400, "invalid_request", "能力开关必须是布尔值")
        if "delegation" in b or b.get("suggestions"):
            return problem(400, "invalid_request", "主机版未启用委派和下一步预测")
        atts = b.get("attachments", [])
        if not isinstance(atts, list) or len(atts) > 64 or not all(isinstance(p, str) and ATTACH_PATH.fullmatch(p) for p in atts):
            return problem(400, "invalid_request", "附件不对")
        delivery = b.get("delivery", "")
        if delivery not in ("", "queue", "steer"):
            return problem(400, "invalid_request", "delivery 只能是 queue 或 steer")
        if b.get("scope") != self.chat_scope:
            return problem(409, "chat_scope_changed", "消息所属的实例或登录身份已变化")
        # 缺省值与上游 Go 的零值一致，JSON 键顺序不参与指纹。
        canonical = {k: b.get(k, []) if k == "attachments" else b.get(k, False) if k in
                     ("questions", "suggestions", "tasks") else b.get(k, "")
                     for k in INPUT_KEYS - {"delegation"}}
        fp = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        r = self.receipts.items.get(rid)
        if r:
            if r["session_id"] != w["id"] or r["state"] in ("deleted", "abandoned"):
                return problem(409, "chat_request_gone", "这条消息编号已经封存")
            # 兼容修复前已持久化的指纹。
            if r.get("_fp") in (fp, hashlib.sha256(dumps(sorted(b.items())).encode()).hexdigest()):
                return js({"version": 1, "receipt": Receipts.public(self.recover_orphan(r)), "replayed": True},
                          headers={"Cache-Control": "no-store"})
            return problem(409, "chat_request_conflict", "消息编号与已接收内容不一致")
        tid = b["thread_id"]
        if not self.thread_exists(w, tid):
            return problem(409, "chat_thread_changed", "消息所属对话已不存在")
        if not b["text"].strip():
            return problem(400, "invalid_request", "消息不能是空的")
        if not self.hub.valid_model(b["model"]) or b["effort"] not in ("", *EFFORT_LEVELS) or b["effort_control"] not in ("", "effort"):
            return problem(400, "invalid_request", "模型或思考强度不支持")
        if not all(self.hub.files.valid_attachment(p) for p in atts):
            return problem(400, "chat_attachments_invalid", "消息附件已失效或无法验证")
        s = self.hub.sessions.get(tid)
        blocked = self.blocked(w["id"], tid)
        busy = s and s.id in self.hub.tasks
        queued = any(x["session_id"] == w["id"] and x["thread_id"] == tid and x["state"] == "queued"
                     for x in self.receipts.items.values())
        parallel = self.busy_count(w) >= self.max_parallel()
        if not delivery and (blocked or busy or queued or parallel):
            code = "chat_pending" if blocked or queued else "chat_busy" if busy else "chat_parallel_limit"
            return problem(409, code, "本对话有未结束的消息或同时运行的对话已达上限")
        t = now_iso()
        enqueue = bool(blocked or busy or queued or parallel)
        # 插话交给原引擎；CLI 尚未连上时作为排队消息保存。
        steer = delivery == "steer" and busy and tid in self.hub.active
        r = {"request_id": rid, "session_id": w["id"], "thread_id": tid,
             "turn_id": "" if steer else secrets.token_hex(16), "request": b,
             "state": "accepted" if steer or not enqueue else "queued", "revision": 1,
             "created_at": t, "updated_at": t, "_fp": fp, "_web_queue": True}
        self.receipts.items[rid] = r
        self.receipts.save()
        self.broadcast(w["id"], {"type": "chat_request", "version": 1, "receipt": Receipts.public(r)})
        if r["state"] == "accepted":
            try:
                if steer:
                    if not await self.hub.steer(s, self.to_host(b["text"].strip()), rid):
                        self.receipt_set(rid, state="queued", turn_id=secrets.token_hex(16))
                else:
                    await self.send_message(w, b, request_id=rid, thread=tid)
            except Exception:
                self.receipt_set(rid, state="uncertain", error_code="execution_incomplete")
        if r["state"] == "queued":
            self.hub.spaces.start(w)
            if s and s.id not in self.hub.tasks:
                s.queue_paused = False
                self.hub.save()
            self.kick_queue()
        return js({"version": 1, "receipt": Receipts.public(r), "replayed": False}, status=202,
                  headers={"Cache-Control": "no-store"})

    async def request_get(self, request):
        w = self.ws(request.match_info["id"])
        r = self.receipts.items.get(request.match_info["request"])
        if not r or r["session_id"] != w["id"]:
            return problem(404, "chat_request_not_found", "尚未查到该消息的接收记录")
        return js({"version": 1, "receipt": Receipts.public(self.recover_orphan(r))}, headers={"Cache-Control": "no-store"})

    async def request_list(self, request):
        w = self.ws(request.match_info["id"])
        mine = [r for r in self.receipts.items.values() if r["session_id"] == w["id"]]
        for r in mine:
            self.recover_orphan(r)
        if request.query.get("pending_only") == "1":
            rows = sorted((r for r in mine if r["state"] == "queued"), key=lambda r: r["created_at"])[:50]
        else:
            thread = request.query.get("thread")
            rows = sorted((r for r in mine if not thread or r["thread_id"] == thread), key=lambda r: r["created_at"],
                          reverse=True)[:50]
        pendings = sorted((r for r in mine if r["state"] in (*ACTIVE, "uncertain")), key=lambda r: r["created_at"])
        pending = next((r for r in pendings if r["thread_id"] == request.query.get("thread")),
                       pendings[0] if pendings else None)
        return js({"version": 1, "scope": self.chat_scope, "requests": [Receipts.public(r) for r in rows],
                   "pending": Receipts.public(pending), "pendings": [Receipts.public(r) for r in pendings]},
                  headers={"Cache-Control": "no-store"})

    async def request_action(self, request):
        async with self.submit_lock:
            return await self.act_request(request)

    async def act_request(self, request):
        w = self.ws(request.match_info["id"])
        rid = request.match_info["request"]
        b = await self.api.body(request)
        action = b.get("action")
        r = self.receipts.items.get(rid)
        if not RID.fullmatch(rid) or set(b) - {"scope", "action", "revision"}:
            return problem(400, "invalid_request", "消息操作无效")
        if b.get("scope") != self.chat_scope:
            return problem(409, "chat_scope_changed", "消息所属的实例或登录身份已变化")
        if r and r["session_id"] != w["id"]:
            return problem(409, "chat_request_conflict", "消息不属于这个空间")
        if action == "abandon":
            if r is None:
                t = now_iso()
                r = {"request_id": rid, "session_id": w["id"], "thread_id": "", "turn_id": "", "request": None,
                     "state": "abandoned", "error_code": "abandoned", "revision": 1, "created_at": t, "updated_at": t}
                self.receipts.items[rid] = r
                self.receipts.save()
            elif r["state"] != "abandoned":
                return problem(409, "chat_request_conflict", "这条消息已经被接收，不能放弃")
        elif r is None:
            return problem(404, "chat_request_not_found", "尚未查到该消息的接收记录")
        else:
            revision = b.get("revision")
            replay = (action == "review" and r["state"] == "reviewed" or action == "withdraw" and
                      r["state"] == "abandoned" and r.get("error_code") == "withdrawn") and revision in (
                          r["revision"], r["revision"] - 1)
            if replay:
                return js({"version": 1, "receipt": Receipts.public(r)})
            if not isinstance(revision, int) or isinstance(revision, bool) or revision != r["revision"]:
                return problem(409, "chat_request_conflict", "消息状态已经变了，请查询后再操作")
            if action == "withdraw":
                if r["state"] != "queued":
                    return problem(409, "chat_request_conflict", "消息已经开始执行，不能撤回")
                if r.get("_web_queue"):
                    self.receipt_set(rid, state="abandoned", error_code="withdrawn")
                else:
                    try:
                        await self.hub.cmd_withdraw({"id": r["thread_id"], "qid": rid})
                    except self.Error as e:
                        return problem(409, "chat_request_conflict", str(e))
            elif action == "interrupt":
                if r["state"] not in ACTIVE or self.running.get(r["thread_id"]) != rid:
                    return problem(409, "chat_request_conflict", "这条消息已经结束了")
                try:
                    await self.hub.cmd_stop({"id": r["thread_id"], "via": "web"})
                except self.Error as e:
                    return problem(409, "chat_request_conflict", str(e))
            elif action == "review":
                if r["state"] != "uncertain" or r["thread_id"] in self.hub.tasks:
                    return problem(409, "chat_request_conflict", "只有已结束且结果不确定的消息需要核对")
                self.receipt_set(rid, state="reviewed")
                self.kick_queue()
            else:
                return problem(400, "invalid_request", "不认识的操作")
        return js({"version": 1, "receipt": Receipts.public(r)}, headers={"Cache-Control": "no-store"})

    async def question_answer(self, request):
        w = self.ws(request.match_info["id"])
        qid = request.match_info["question"]
        waiter = self.hub.waiters.get(qid[3:] if qid.startswith("ap-") else qid)
        if not waiter or not self.thread_exists(w, waiter.sid):
            return problem(409, "question_closed", "这个问题已经结束或不属于本空间")
        b = await self.api.body(request)
        skip = bool(b.get("skip"))
        answers = b.get("answers") or {}
        if not skip and (not isinstance(answers, dict) or not answers):
            return problem(400, "invalid_request", "每个问题都要有回答")
        if not isinstance(b.get("skip", False), bool) or not isinstance(answers, dict) or any(
                not isinstance(k, str) or not isinstance(v, (str, list)) or
                isinstance(v, str) and len(v) > 4000 or isinstance(v, list) and
                (len(v) > 64 or not all(isinstance(x, str) and len(x) <= 4000 for x in v))
                for k, v in answers.items()):
            return problem(400, "invalid_request", "回答格式无效")
        try:
            if qid.startswith("ap-"):   # 包装成提问的工具审批
                qs = self.qcache.get(qid)
                if not qs:
                    # 重启不会留下活审批；多页面也可根据同一 waiter 还原原文。
                    qs = [{"question": approval_question({"tool": waiter.tool, "summary": waiter.summary, "path": waiter.path})}]
                allow = not skip and answers.get(qs[0]["question"]) == APPROVE
                await self.hub.decide({"req": qid[3:], "via": "web", "reason": "" if allow else "网页上拒绝了"}, allow)
                state = "skipped" if skip else "answered"
            else:
                res = await self.hub.cmd_answer({"qid": qid, "answers": dict(answers), "skip": skip, "via": "web"})
                state = "skipped" if res.get("skipped") else "answered"
        except self.Error as e:
            if getattr(e, "status", 400) == 409:
                return problem(409, "question_closed", "这个问题已经结束了")
            return problem(400, "invalid_request", str(e))
        return js({"version": 1, "state": state})


def approval_question(e):
    summary = (e.get("summary") or "").strip()
    extra = f"\n路径：{e['path']}" if e.get("path") else ""
    return f"允许 Claude 使用 {e.get('tool')}？\n{summary}{extra}".strip()


def transcript_path(s):
    try:
        from claude_agent_sdk._internal.sessions import _find_project_dir
        d = _find_project_dir(s.cwd)
        return str(d / f"{s.id}.jsonl") if d else None
    except Exception:
        return None


def slim_old(ev):
    """旧段：截图不留（一张几百 KB），跟 tool_result 正文重复的大字段截短（stdout、读文件全文），diff 等结构化信息保留。"""
    for blk in (ev.get("message") or {}).get("content") or []:
        if isinstance(blk, dict) and blk.get("type") == "tool_result" and isinstance(blk.get("content"), list):
            blk["content"] = [{"type": "text", "text": "（旧记录里的图片，不再显示）"}
                              if isinstance(x, dict) and x.get("type") == "image" else x for x in blk["content"]]
    tur = ev.get("tool_use_result")
    if isinstance(tur, dict):
        if isinstance(tur.get("file"), dict) and "base64" in tur["file"]:
            tur["file"] = {k: v for k, v in tur["file"].items() if k != "base64"}
        for k in ("stdout", "stderr", "content", "originalFile"):
            if isinstance(tur.get(k), str) and len(tur[k]) > 2000:
                tur[k] = tur[k][:2000] + "\n…（旧记录，后面省略）"
        if isinstance(tur.get("file"), dict) and isinstance(tur["file"].get("content"), str):
            tur["file"]["content"] = tur["file"]["content"][:2000]
    elif isinstance(tur, str) and len(tur) > 2000:
        ev["tool_use_result"] = tur[:2000]
    return ev


def read_transcript_full(s):
    """跟 read_transcript 一样串当前分支，但遇到压缩边界接着往前走（logicalParentUuid），压缩前的内容也要；
    压缩生成的摘要消息不显示。"""
    from claude_agent_sdk._internal import sessions as sx
    content = sx._read_session_file(s.id, s.cwd)
    if not content:
        return []
    entries = sx._parse_transcript_entries(content)
    chain = sx._build_conversation_chain(entries)
    if not chain:
        return []
    by_uuid = {e["uuid"]: e for e in entries}
    out, seen = [], set()
    cur = by_uuid.get(chain[-1]["uuid"])
    while cur is not None and cur["uuid"] not in seen:
        seen.add(cur["uuid"])
        out.append(cur)
        parent = cur.get("parentUuid")
        if not parent and cur.get("type") == "system" and cur.get("subtype") == "compact_boundary":
            parent = cur.get("logicalParentUuid")
        cur = by_uuid.get(parent) if parent else None
    out.reverse()
    out = sx._recover_orphaned_parallel_tool_results(by_uuid, out, seen)
    return [e for e in out if sx._is_visible_message(e) and not e.get("isCompactSummary")]


def read_transcript(s):
    """Claude Code 的会话文件 → 当前分支上要显示的条目（带时间戳和 toolUseResult）。用 SDK 自己串对话链的逻辑。"""
    from claude_agent_sdk._internal import sessions as sx
    content = sx._read_session_file(s.id, s.cwd)
    if not content:
        return []
    chain = sx._build_conversation_chain(sx._parse_transcript_entries(content))
    return [e for e in chain if sx._is_visible_message(e)]


def is_tool_result(msg):
    content = msg.get("content")
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def user_text(msg):
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def latest_todos(s):
    """会话里最近一次 TodoWrite 的任务列表（老模型的任务面板）。"""
    todos = []
    for e in read_transcript(s):
        msg = e.get("message") or {}
        if e.get("type") != "assistant":
            continue
        for b in msg.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "TodoWrite":
                todos = (b.get("input") or {}).get("todos") or []
    return todos


def register(app, api):
    c = Compat(api, api.token)
    api.hub.abx = c
    async def start_queue(app):
        c.kick_queue()

    async def close_rooms(app):
        if c.dispatch_task:
            c.dispatch_task.cancel()
            await asyncio.gather(c.dispatch_task, return_exceptions=True)
        for queues in c.rooms.values():
            for q in list(queues):
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)

    async def prewarm(app):
        """重启（部署）后第一次打开长对话要整段冷构建，几十 MB 的会话要好几秒；后台先把最近的几个建好。"""
        async def run():
            await asyncio.sleep(3)
            recent = sorted(c.hub.sessions.values(), key=lambda s: s.updated or 0, reverse=True)[:3]
            for s in recent:
                try:
                    await asyncio.to_thread(c.history_cached, s)
                except Exception:   # 预热失败无所谓，打开时照常现建
                    pass
        c.prewarm_task = asyncio.create_task(run())

    async def stop_prewarm(app):
        task = getattr(c, "prewarm_task", None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app.on_startup.append(start_queue)
    app.on_startup.append(prewarm)
    app.on_shutdown.append(close_rooms)
    app.on_shutdown.append(stop_prewarm)
    r = app.router
    r.add_post("/api/login", c.login)
    r.add_post("/api/logout", c.logout)
    r.add_get("/api/ping", c.ping)
    r.add_get("/api/icons", c.icons)
    r.add_get("/api/me", c.me)
    # 上游用来记「最后活跃」给管理员看；单人版不记，照上游回 204，免得每次点击都留一条 404
    r.add_post("/api/me/activity", c.activity)
    r.add_get("/api/sessions/{id}/models", c.models)
    r.add_get("/api/sessions/{id}/chat/threads", c.threads)
    r.add_post("/api/sessions/{id}/chat/threads", c.thread_new)
    r.add_post("/api/sessions/{id}/chat/reset", c.thread_new)
    r.add_post("/api/sessions/{id}/chat/threads/{tid}/activate", c.thread_activate)
    r.add_patch("/api/sessions/{id}/chat/threads/{tid}", c.thread_patch)
    r.add_delete("/api/sessions/{id}/chat/threads/{tid}", c.thread_delete)
    r.add_get("/api/sessions/{id}/history", c.history)
    r.add_get("/api/sessions/{id}/chat", c.chat_ws)
    r.add_put("/api/sessions/{id}/chat/requests/{request}", c.request_put)
    r.add_get("/api/sessions/{id}/chat/requests/{request}", c.request_get)
    r.add_post("/api/sessions/{id}/chat/requests/{request}", c.request_action)
    r.add_get("/api/sessions/{id}/chat/requests", c.request_list)
    r.add_post("/api/sessions/{id}/chat/questions/{question}", c.question_answer)
    r.add_get("/api/sessions/{id}/chat/tasks", c.tasks)
    r.add_get("/api/sessions/{id}/outline", c.outline)
    r.add_get("/api/sessions/{id}/commands", c.commands)
