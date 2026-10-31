"""
agentbox 兼容接口（2026-10-26）：网页端直接用 agentbox（devilcoolyue/agentbox，Apache-2.0）的前端，
文件在 web/abx/，这里按它的接口格式（web/src/types.d.ts、docs/chat-protocol.md）提供对话相关的 /api/…，
背后接 hub 自己的会话引擎。文件、Git、终端、技能、MCP、空间管理在 hub_files / hub_git / hub_term /
hub_skills / hub_mcp / hub_spaces 里。

对应关系
- agentbox 的「工作空间」（它叫 Session）= hub 的工作空间（一个项目目录）。没有容器，Agent 直接跑在 lab 上。
- agentbox 的「对话线程」（Thread）= 一个 Claude Code 会话（hub 的 Session）。新对话在发第一条消息之前
  就先分好 UUID（草稿、回执都按它归属），发消息时用这个 UUID 建 Claude Code 会话。
- 对话历史 = Claude Code 的会话文件为主干，穿插 hub 才有的耗时、费用、提问、审批、插话。
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
    return js({"ok": False, "code": code, "error": message}, status=status, headers={"Cache-Control": "no-store"})


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
                r.update(state="failed", error_code="server_restarted", revision=r["revision"] + 1,
                         updated_at=now_iso())
                changed = True
        if changed:
            self.save()

    def save(self):
        if len(self.items) > 600:
            done = sorted((r for r in self.items.values() if r["state"] != "queued"), key=lambda r: r["created_at"])
            for r in done[:len(self.items) - 500]:
                del self.items[r["request_id"]]
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.items, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

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
        self.history_memo: dict[str, tuple] = {}   # 会话 id → (版本, (整段历史, 费用))
        state_dir = self.hub.files.state_dir
        self.receipts = Receipts(os.path.join(state_dir, "abx-receipts.json"))
        ident = f"agent-hub/{OWNER}/{state_dir}"
        self.chat_scope = hashlib.sha256((ident + "/chat").encode()).hexdigest()
        self.draft_scope = hashlib.sha256((ident + "/draft").encode()).hexdigest()
        self.hub.listeners.append(self.on_hub_event)

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
        return js({})

    async def ping(self, request):
        return web.Response(text="pong")

    async def icons(self, request):
        return js({})

    async def me(self, request):
        return js({
            "user": OWNER, "role": "admin", "models": {"claude": self.model_options()}, "terminal_tips": None,
            "timezone": TIMEZONE, "quota": None, "chat_history_timeout_sec": 120,
            "draft_scope": self.draft_scope, "draft_protocol": 1, "chat_protocol": 1, "chat_scope": self.chat_scope,
            "delegation_protocol": 0, "prompt_suggestions": 0, "chat_questions": 1, "chat_followups": 1,
            "chat_tasks": 1,
        })

    # ---- 对话辅助：任务列表、大纲、/ 命令、模型 ----

    def thread_session(self, request):
        w = self.ws(request.match_info["id"])
        tid = request.query.get("thread") or self.active(w)
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
        entries, _ = await asyncio.to_thread(self.history_cached, s)
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
        return js({"models": self.model_options(), "discovery": "available", "default_model": DEFAULT_MODEL,
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
        if tid and (tid in self.hub.sessions or tid == w.get("pending_thread")):
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
        if active not in self.hub.sessions:
            items.insert(0, self.thread(None, active))
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
        if tid not in self.hub.sessions and tid != w.get("pending_thread"):
            raise self.Error("对话不存在", status=404)
        self.set_active(w, tid)
        s = self.hub.sessions.get(tid)
        return js({"id": tid, "resumable": bool(s and s.started)})

    async def thread_patch(self, request):
        s = self.hub.sessions.get(request.match_info["tid"])
        if not s:
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
        s = self.hub.sessions.get(tid)
        if s:
            if s.id in self.hub.tasks:
                raise self.Error("这个对话还在跑，先停下来", status=409)
            self.hub.sessions.pop(s.id)   # 只从 hub 移除，Claude Code 自己的会话记录还在
            self.hub.save()
        if w.get("active_thread") == tid or tid == w.get("pending_thread"):
            rest = self.threads_of(w)
            self.set_active(w, rest[0].id if rest else self.pending_thread(w, fresh=True))
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
        """分页：先给最近的一页，页面往上滚时带 before=<上一页起点> 要更早的（照 agentbox 的翻页协议）。
        页的起点尽量对齐到一条用户消息，一轮不会被切成两半。"""
        w = self.ws(request.match_info["id"])
        active = self.active(w)
        tid = request.query.get("thread") or active
        s = self.hub.sessions.get(tid)
        full, costs = await asyncio.to_thread(self.history_cached, s) if s else ([], {})
        try:
            end = min(max(int(request.query["before"]), 0), len(full)) if "before" in request.query else len(full)
        except ValueError:
            end = len(full)
        start = max(0, end - HISTORY_PAGE)
        i = start
        while i > 0 and full[i]["kind"] != "user" and end - i < 2 * HISTORY_PAGE:
            i -= 1
        if i < start and full[i]["kind"] == "user":
            start = i
        return js({"active_thread": active, "entries": full[start:end], "thread": self.thread(s, tid), "thread_id": tid,
                   "costs": costs, "start": start, "has_more": start > 0})

    def history_cached(self, s):
        """整段历史按（会话文件大小 + 修改时间 + hub 事件序号）缓存，长对话翻页时不用每页都重读几十 MB。"""
        path = transcript_path(s)
        try:
            st = os.stat(path) if path else None
        except OSError:
            st = None
        key = (st.st_size, st.st_mtime) if st else None, self.hub.log_of(s.id).seq
        hit = self.history_memo.get(s.id)
        if hit and hit[0] == key:
            return hit[1]
        value = self.build_history(s)
        self.history_memo[s.id] = (key, value)
        while len(self.history_memo) > 3:
            self.history_memo.pop(next(iter(self.history_memo)))
        return value

    def build_history(self, s):
        hub_events = self.hub.log_of(s.id).read()
        user_turns = [e for e in hub_events if e["kind"] == "user"]
        entries = []
        transcript = read_transcript(s)
        if transcript:
            prompts = iter(user_turns)
            pending = next(prompts, None)
            for e in transcript:
                ts = e.get("timestamp") or ""
                msg = e.get("message") or {}
                if e.get("type") == "user" and not is_tool_result(msg):
                    if (e.get("origin") or {}).get("kind", "human") != "human":
                        continue   # 后台任务通知之类插进来的消息，不算机主的话
                    text = user_text(msg)
                    if not text:
                        continue
                    entry = {"kind": "user", "text": text, "ts": ts}
                    if pending and pending.get("text", "").strip() == text.strip():   # 对上 hub 记的那一轮，拿到 turn 元数据
                        entry["turn"] = pending.get("turn")
                        pending = next(prompts, None)
                    entries.append(entry)
                else:
                    ev = {"type": e["type"], "message": msg, "parent_tool_use_id": None}
                    if e.get("toolUseResult") is not None:
                        ev["tool_use_result"] = e["toolUseResult"]
                    entries.append({"kind": "event", "event": ev, "ts": ts})
        else:   # 还没有会话文件（刚发出第一条）：只有 hub 自己记的
            for e in user_turns:
                entries.append({"kind": "user", "text": e.get("text", ""), "ts": iso(e["ts"]), "turn": e.get("turn")})
        costs, turn_id, extra, steers = {}, None, [], {}
        questions = {}
        for e in hub_events:
            k, ts = e["kind"], iso(e["ts"])
            if k == "user":
                turn_id = (e.get("turn") or {}).get("id")
            elif k == "result":
                if turn_id and e.get("cost"):
                    costs[turn_id] = {"turn_id": turn_id, "cost_micro_usd": round(e["cost"] * 1e6), "source": "provider"}
                extra.append({"kind": "status", "state": e.get("status") or "idle", "error": e.get("error") or "",
                              "duration_ms": round((e.get("secs") or 0) * 1000), "ts": ts})
            elif k in ("question", "question_resolved", "approval", "approval_resolved"):
                view = self.question_view(e, questions)
                if view:
                    extra.append({"kind": "question", "event": view, "ts": ts})
            elif k == "steer":
                view = self.steer_view(e)
                if view["state"] in ("applied", "dropped"):
                    steers[view["id"]] = {"kind": "steer", "event": view, "ts": steers.get(view["id"], {}).get("ts", ts)}
        merged = sorted(entries + extra + list(steers.values()), key=lambda x: x.get("ts") or "")
        return json.loads(self.virtualize(dumps(merged), s.cwd)), costs

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
            self.receipt_set(qid, state="abandoned", error_code="withdrawn", request=None)
        elif k == "steer" and ev.get("state") == "sent" and qid in self.receipts.items:
            self.receipt_set(qid, state="completed", turn_id="", merged_into=self.running.get(s.id) or "0" * 32)
        elif k == "result":
            rid = self.running.pop(s.id, None)
            if rid:
                state, code = RESULT_STATE.get(ev.get("status") or "idle", ("failed", "turn_failed"))
                self.receipt_set(rid, state=state, **({"error_code": code} if code else {}))

    def on_hub_event(self, s, ev):
        if ev["kind"] in ("user", "queued", "dequeued", "withdrawn", "steer", "result"):
            self.track_receipts(s, ev)
        if ev["kind"] == "user":
            self.turn_ids[s.id] = (ev.get("turn") or {}).get("id")
        w = self.hub.workspaces.by_path(s.cwd)
        if not w or not self.rooms.get(w["id"]) or self.active(w) != s.id:
            return   # 页面只显示当前对话线程
        k, ts = ev["kind"], iso(ev["ts"])
        out = []
        if k == "user":
            out.append({"type": "user_message", "text": ev.get("text", ""), "ts": ts, "turn": ev.get("turn")})
        elif k == "agent_event":
            out.append({"type": "agent_event", "event": ev["event"], "ts": ts})
        elif k == "status":
            st = ev.get("status")
            if st in ("running", "waiting"):
                if st == "running":
                    out.append({"type": "status", "state": "running", "ts": ts, "phase": ""})
            else:
                out.append({"type": "status", "state": st, "error": ev.get("error") or "", "ts": ts})
        elif k == "result":
            tid = self.turn_ids.get(s.id)
            if tid and ev.get("cost") is not None:
                out.append({"type": "turn_cost", "running": False,
                            "cost": {"turn_id": tid, "cost_micro_usd": round((ev.get("cost") or 0) * 1e6),
                                     "source": "provider"}})
            out.append({"type": "status", "state": ev.get("status") or "idle", "error": ev.get("error") or "",
                        "duration_ms": round((ev.get("secs") or 0) * 1000), "ts": ts})
        elif k in ("question", "question_resolved", "approval", "approval_resolved"):
            view = self.question_view(ev, self.qcache)
            if view:
                out.append({"type": "question", "event": view, "ts": ts})
        elif k == "steer":
            view = self.steer_view(ev)
            if view["state"] in ("applied", "dropped"):
                out.append({"type": "steer", "event": view, "ts": ts})
        for msg in out:
            self.broadcast(w["id"], msg)

    def broadcast(self, wid, msg):
        for q in list(self.rooms.get(wid, ())):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    async def chat_ws(self, request):
        w = self.ws(request.match_info["id"])
        wsr = web.WebSocketResponse(heartbeat=30, max_msg_size=4 << 20)
        await wsr.prepare(request)
        q = asyncio.Queue(maxsize=5000)
        self.rooms.setdefault(w["id"], set()).add(q)

        async def writer():
            while True:
                msg = await q.get()
                await wsr.send_str(self.virtualize(dumps(msg), w["path"]))

        wtask = asyncio.create_task(writer())
        try:
            s = self.hub.sessions.get(self.active(w))
            status = {"type": "status", "state": "idle"}
            if s and s.id in self.hub.tasks:
                status = {"type": "status", "state": "running", "phase": "",
                          "elapsed_ms": round((time.time() - s.updated) * 1000)}
            await wsr.send_str(dumps(status))
            async for msg in wsr:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                try:
                    if data.get("type") == "user_message":   # 旧发送路径（没有编号）
                        await self.send_message(w, data)
                    elif data.get("type") == "interrupt":
                        s = self.hub.sessions.get(self.active(w))
                        if s and s.id in self.hub.active:
                            await self.hub.cmd_stop({"id": s.id, "via": "web"})
                except self.Error as e:
                    await wsr.send_str(dumps({"type": "error", "error": str(e)}))
        finally:
            self.rooms.get(w["id"], set()).discard(q)
            wtask.cancel()
        return wsr

    async def send_message(self, w, data, request_id=None, delivery=None):
        text = self.to_host((data.get("text") or "").strip())
        if not text:
            return None
        model = data.get("model") or None
        effort = data.get("effort") if data.get("effort") is not None else None
        tasks = bool(data.get("tasks")) if "tasks" in data else None
        sid = self.active(w)
        if sid not in self.hub.sessions:   # 空对话的第一条：用先分好的 UUID 建 Claude Code 会话
            r = await self.hub.cmd_new({"id": sid, "cwd": w["path"], "prompt": text, "model": model or "",
                                        "effort": effort or "", "mode": w.get("mode") or "bypassPermissions",
                                        "request_id": request_id, "task_tools": tasks})
            w.pop("pending_thread", None)
            self.set_active(w, sid)
            return r
        s = self.hub.sessions[sid]
        if (model is not None and model != s.model) or (effort is not None and effort != s.effort):
            await self.hub.cmd_model({"id": sid, "model": model if model is not None else s.model,
                                      "effort": effort if effort is not None else s.effort})
        return await self.hub.cmd_send({"id": sid, "prompt": text, "request_id": request_id, "delivery": delivery,
                                        "task_tools": tasks})

    # ---- 消息回执（chat_protocol 1）----

    async def request_put(self, request):
        w = self.ws(request.match_info["id"])
        rid = request.match_info["request"]
        if not RID.match(rid):
            return problem(400, "invalid_request", "消息编号不对")
        try:
            b = await request.json()
        except ValueError:
            return problem(400, "invalid_request", "请求体不是 JSON")
        if not isinstance(b, dict) or set(b) - INPUT_KEYS:
            return problem(400, "invalid_request", "请求里有不认识的字段，请刷新页面")
        if not isinstance(b.get("text"), str) or len(b["text"].encode()) > 1 << 20:
            return problem(400, "invalid_request", "消息正文不对或太长")
        atts = b.get("attachments") or []
        if not isinstance(atts, list) or len(atts) > 64 or not all(isinstance(p, str) and ATTACH_PATH.match(p)
                                                                    for p in atts):
            return problem(400, "invalid_request", "附件不对")
        fp = hashlib.sha256(dumps(sorted(b.items())).encode()).hexdigest()
        r = self.receipts.items.get(rid)
        if r:
            if r.get("_fp") == fp and r["session_id"] == w["id"]:
                return js({"version": 1, "receipt": Receipts.public(r), "replayed": True},
                          headers={"Cache-Control": "no-store"})
            return problem(409, "chat_request_conflict", "消息编号与已接收内容或状态不一致")
        if b.get("scope") != self.chat_scope:
            return problem(409, "chat_scope_changed", "消息所属的实例或登录身份已变化")
        active = self.active(w)
        if b.get("thread_id") != active:
            return problem(409, "chat_thread_changed", "当前对话与消息所属对话不一致")
        if not b["text"].strip():
            return problem(400, "invalid_request", "消息不能是空的")
        delivery = b.get("delivery")
        if delivery not in (None, "queue", "steer"):
            return problem(400, "invalid_request", "delivery 只能是 queue 或 steer")
        s = self.hub.sessions.get(active)
        if not delivery and s and s.id in self.hub.tasks:
            return problem(409, "chat_busy", "上一条消息仍在处理中")
        if not delivery and s and s.queue:
            return problem(409, "chat_pending", "前面还有排队的消息")
        shared = self.hub.files.shared
        if not all(os.path.isfile(os.path.join(shared, p[len("/shared/"):])) for p in atts):
            return problem(400, "chat_attachments_invalid", "消息附件已失效或无法验证")
        t = now_iso()
        r = {"request_id": rid, "session_id": w["id"], "thread_id": active, "turn_id": "", "request": b,
             "state": "accepted", "revision": 1, "created_at": t, "updated_at": t, "_fp": fp}
        self.receipts.items[rid] = r
        self.receipts.save()
        try:
            await self.send_message(w, b, request_id=rid, delivery=delivery)
        except self.Error as e:
            del self.receipts.items[rid]   # 没开始执行：编号不占，改了之后原编号还能用
            self.receipts.save()
            return problem(getattr(e, "status", 400), "invalid_request", str(e))
        return js({"version": 1, "receipt": Receipts.public(r), "replayed": False}, status=202,
                  headers={"Cache-Control": "no-store"})

    async def request_get(self, request):
        w = self.ws(request.match_info["id"])
        r = self.receipts.items.get(request.match_info["request"])
        if not r or r["session_id"] != w["id"]:
            return problem(404, "chat_request_not_found", "尚未查到该消息的接收记录")
        return js({"version": 1, "receipt": Receipts.public(r)}, headers={"Cache-Control": "no-store"})

    async def request_list(self, request):
        w = self.ws(request.match_info["id"])
        mine = [r for r in self.receipts.items.values() if r["session_id"] == w["id"]]
        if request.query.get("pending_only") == "1":
            rows = sorted((r for r in mine if r["state"] == "queued"), key=lambda r: r["created_at"])[:50]
        else:
            thread = request.query.get("thread")
            rows = sorted((r for r in mine if not thread or r["thread_id"] == thread), key=lambda r: r["created_at"],
                          reverse=True)[:50]
        pending = next((r for r in sorted(mine, key=lambda r: r["created_at"], reverse=True)
                        if r["state"] in ACTIVE or r["state"] == "uncertain"), None)
        return js({"version": 1, "scope": self.chat_scope, "requests": [Receipts.public(r) for r in rows],
                   "pending": Receipts.public(pending)}, headers={"Cache-Control": "no-store"})

    async def request_action(self, request):
        w = self.ws(request.match_info["id"])
        rid = request.match_info["request"]
        b = await self.api.body(request)
        action = b.get("action")
        r = self.receipts.items.get(rid)
        if b.get("scope") not in (None, self.chat_scope):
            return problem(409, "chat_scope_changed", "消息所属的实例或登录身份已变化")
        if action == "abandon":
            if not RID.match(rid):
                return problem(400, "invalid_request", "消息编号不对")
            if r is None:
                t = now_iso()
                r = {"request_id": rid, "session_id": w["id"], "thread_id": "", "turn_id": "", "request": None,
                     "state": "abandoned", "error_code": "abandoned", "revision": 1, "created_at": t, "updated_at": t}
                self.receipts.items[rid] = r
                self.receipts.save()
            elif r["state"] != "abandoned":
                return problem(409, "chat_request_conflict", "这条消息已经被接收，不能放弃")
            return js({"version": 1, "receipt": Receipts.public(r)})
        if r is None or r["session_id"] != w["id"]:
            return problem(404, "chat_request_not_found", "尚未查到该消息的接收记录")
        if b.get("revision") is not None and b["revision"] != r["revision"] and action != "review":
            return problem(409, "chat_request_conflict", "消息状态已经变了，请刷新后再操作")
        if action == "withdraw":
            if r["state"] != "queued":
                return problem(409, "chat_request_conflict", "这条消息已经开始执行，不能撤回")
            try:
                await self.hub.cmd_withdraw({"id": r["thread_id"], "qid": rid})
            except self.Error as e:
                return problem(409, "chat_request_conflict", str(e))
        elif action == "interrupt":
            if r["state"] not in ACTIVE:
                return problem(409, "chat_request_conflict", "这条消息已经结束了")
            try:
                await self.hub.cmd_stop({"id": r["thread_id"], "via": "web"})
            except self.Error as e:
                return problem(409, "chat_request_conflict", str(e))
        elif action == "review":
            if r["state"] == "uncertain":
                self.receipt_set(rid, state="reviewed")
            elif r["state"] != "reviewed":
                return problem(409, "chat_request_conflict", "只有结果不确定的消息需要核对")
        else:
            return problem(400, "invalid_request", "不认识的操作")
        return js({"version": 1, "receipt": Receipts.public(self.receipts.items[rid])})

    async def question_answer(self, request):
        self.ws(request.match_info["id"])
        qid = request.match_info["question"]
        b = await self.api.body(request)
        skip = bool(b.get("skip"))
        answers = b.get("answers") or {}
        if not skip and (not isinstance(answers, dict) or not answers):
            return problem(400, "invalid_request", "每个问题都要有回答")
        try:
            if qid.startswith("ap-"):   # 包装成提问的工具审批
                allow = not skip and any(v == APPROVE for v in answers.values())
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
    r = app.router
    r.add_post("/api/login", c.login)
    r.add_post("/api/logout", c.logout)
    r.add_get("/api/ping", c.ping)
    r.add_get("/api/icons", c.icons)
    r.add_get("/api/me", c.me)
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
