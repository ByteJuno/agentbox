"""
工作空间和外壳（2026-10-27）：agentbox 前端里跟「对话」无关的那些接口——空间列表与新建 / 改名 / 删除 / 停止、
账号、使用记录、设置、资源监控、主题、通告、升级、隧道、保活、浏览器。格式照 agentbox 的 web/src/types.d.ts。

- 空间 = lab 上的一个项目目录。新建时「名称」填 ~/ai-work 下已有的文件夹名就直接打开那个项目，
  填 ~/路径 打开 home 下任意目录；不存在就在 ~/ai-work 下新建。只建空目录：上传项目、从 Git 导入
  10-27 按机主的意思删了，入口藏在 web/hub/hub.css，要拉仓库让 Claude 去 clone。
- 「停止」= 结束这个空间的终端会话（里面跑着的程序一起结束）和正在跑的对话；文件和对话记录都在。
  删除空间只是不再登记，不动目录里的文件。
- 账号只有一个：lab 上已登录的 Claude Code。账号池、代理、多用户、额度、内网隧道、镜像更新这些团队功能
  返回空数据或明确的错误，界面上对应入口用 web/hub/hub.css 藏起来。
- 使用记录来自 hub 自己记的每次模型请求（usage.jsonl），费用按 API 价目估算（订阅账号实际不按这个扣）。
"""
import asyncio
import datetime
import hashlib
import json
import os
import re
import threading
import time
import zoneinfo

from aiohttp import web
import hub_theme

from hub_abx import DEFAULT_MODEL, MODEL_OPTIONS, EFFORT_LEVELS, OWNER, TIMEZONE, dumps, iso, js, now_iso

ACCOUNT_ID = "lab"
ACCOUNT_LABEL = "lab 上的 Claude Code"
CREATION_ID = re.compile(r"^[a-f0-9]{32}$")
DIR_NAME = re.compile(r"^[\w.\-]{1,128}$")
SKIN_BASES = ["amber", "glass", "cyberpunk", "graphite", "verdant", "blueprint"]
THEME_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def mem_info():
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                out[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    return out


def cpu_times():
    try:
        with open("/proc/stat") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return sum(parts), idle
    except OSError:
        return 0, 0


def proc_cpu():
    try:
        with open("/proc/self/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return 0.0


class Spaces:
    def __init__(self, hub, state_dir, home, usage_path, prices, error):
        self.hub = hub
        self.Error = error
        self.home = home
        self.state_dir = state_dir
        self.usage_path = usage_path
        self.prices = prices
        self.started = time.time()
        self.creations_path = os.path.join(state_dir, "abx-creations.json")
        self.prefs_path = os.path.join(state_dir, "abx-settings.json")
        self.themes_path = os.path.join(state_dir, "abx-themes.json")
        self.creations = self.load(self.creations_path, {})
        self.live: set[asyncio.Queue] = set()
        self.cpu_prev = (cpu_times(), proc_cpu(), time.time())
        self.usage_cache = (None, [])
        self.lock = asyncio.Lock()
        self.actor_key = hashlib.sha256(f"agent-hub/{OWNER}/{state_dir}/creations".encode()).hexdigest()
        hub.listeners.append(self.on_hub_event)

    @staticmethod
    def load(path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return default

    @staticmethod
    def store(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    # ---- 空间视图 ----

    def short(self, path):
        return "~" + path[len(self.home):] if path == self.home or path.startswith(self.home + os.sep) else path

    def view(self, w):
        v = self.hub.workspaces.view(w)
        return {"id": w["id"], "user": OWNER, "name": w["name"], "agent": "claude", "account_id": ACCOUNT_ID,
                "default_model": w.get("default_model") or DEFAULT_MODEL,
                "status": "stopped" if w.get("stopped") else "running",
                "chat_session": self.hub.abx.active(w), "stop_reason": "",
                "created_at": iso(w.get("created") or time.time()), "updated_at": iso(v["updated"]),
                # agentbox 在这里显示账号名；这里换成目录，列表和顶栏一眼能看出空间对应哪个文件夹
                "account_label": self.short(w["path"]), "extensions": []}

    def ws(self, wid):
        return self.hub.workspaces.get(wid)

    def resources(self):
        return {"cpus": os.cpu_count() or 1, "memory_mb": mem_info().get("MemTotal", 0) // (1 << 20), "pids_limit": 0}

    def account(self):
        n = len(self.hub.workspaces.items)
        reasoning = {m["id"]: {"support": "supported", "control": "effort", "levels": EFFORT_LEVELS}
                     for m in MODEL_OPTIONS}
        return {"id": ACCOUNT_ID, "type": "claude", "label": ACCOUNT_LABEL, "sessions": n, "cred_status": "ok",
                "auth_mode": "oauth", "default_model": DEFAULT_MODEL, "model_reasoning": reasoning,
                "models": [{**m, "reasoning": reasoning[m["id"]]} for m in MODEL_OPTIONS]}

    # ---- 停止 / 启动 / 删除 ----

    async def stop(self, w):
        w["stopped"] = True  # 先阻止调度，再中断，不能在等待期间又起下一条。
        self.hub.workspaces.save()
        for s in [s for s in self.hub.sessions.values() if s.cwd == w["path"] and s.id in self.hub.tasks]:
            try:
                await self.hub.cmd_stop({"id": s.id, "via": "web"})
            except self.Error:
                pass
        await self.hub.terminals.kill(w["id"])
        w["stopped"] = True
        self.hub.workspaces.save()

    def start(self, w):
        if w.pop("stopped", None):
            self.hub.workspaces.save()
        if hasattr(self.hub, "abx"):
            self.hub.abx.kick_queue()

    # ---- 新建空间（agentbox 的创建回执流程）----

    def target(self, name):
        """名称 → 目录：~/… 或 /home/… 是路径；否则是 ~/ai-work 下的文件夹（可以带一层子目录）。"""
        name = name.strip()
        if name.startswith(("~", "/")):
            path = os.path.realpath(os.path.expanduser(name))
            if path != self.home and not path.startswith(self.home + os.sep):
                raise self.Error("只能用 home 下面的目录")
            return path, os.path.basename(path) or path
        base = os.path.join(self.home, "ai-work")
        path = os.path.normpath(os.path.join(base, name))
        if not path.startswith(base + os.sep) or not all(DIR_NAME.match(p) for p in os.path.relpath(path, base).split(os.sep)):
            raise self.Error("名称会当成 ~/ai-work 下的文件夹名：只能用字母、数字、中文、点、横线和下划线；"
                             "要打开别处的目录，名称填 ~/路径")
        return path, name

    def creation_view(self, c):
        w = self.hub.workspaces.items.get(c.get("wid") or "")
        session = self.view(w) if w else {
            "id": "", "user": OWNER, "name": c["request"].get("name") or "", "agent": "claude",
            "account_id": ACCOUNT_ID, "default_model": DEFAULT_MODEL, "status": "stopped", "created_at": c["created_at"],
            "updated_at": c["created_at"], "account_label": ACCOUNT_LABEL}
        return {"request_id": c["request_id"], "request": c["request"], "session": session,
                "git_connection_id": c["request"].get("git_connection_id") or "", "state": c["state"],
                "workspace_exists": bool(w), "busy": bool(c.get("busy")), "imports": c.get("imports", []),
                "created_at": c["created_at"], "container_resources": self.resources()}

    def creation(self, rid):
        c = self.creations.get(rid)
        if not c:
            raise self.Error("没有这条创建记录", status=404)
        return c

    def save_creations(self):
        # 保留原编号，删除/放弃后的旧页面不能重放创建新的空间。
        self.store(self.creations_path, self.creations)

    def reserve(self, rid, spec):
        if spec.get("agent", "claude") != "claude" or spec.get("account_id", ACCOUNT_ID) != ACCOUNT_ID or spec.get("source", "empty") != "empty":
            raise self.Error("主机版只支持已登录的 Claude Code 和空目录创建")
        if not isinstance(spec.get("name"), str):
            raise self.Error("名称必须是文字")
        name = spec["name"].strip()
        if not name:
            raise self.Error("名称不能为空")
        path, display = self.target(name)
        created = False
        if os.path.exists(path) and not os.path.isdir(path):
            raise self.Error(f"{self.short(path)} 是个文件，不是目录")
        if not os.path.isdir(path):
            parent = os.path.dirname(path)
            if not os.path.isdir(parent):
                raise self.Error(f"上级目录 {self.short(parent)} 不存在")
            os.mkdir(path)
            created = True
        w = self.hub.workspaces.ensure(path, display)
        w.setdefault("default_model", self.prefs().get("default_models", {}).get("claude") or DEFAULT_MODEL)
        w.pop("stopped", None)
        self.hub.workspaces.save()
        c = {"request_id": rid, "request": spec, "wid": w["id"], "created_dir": created, "state": "ready",
             "imports": [], "created_at": now_iso()}
        self.creations[rid] = c
        self.save_creations()
        return c

    # ---- 使用记录 ----

    def usage_all(self):
        try:
            st = os.stat(self.usage_path)
        except FileNotFoundError:
            return []
        key = (st.st_mtime, st.st_size)
        if self.usage_cache[0] == key:
            return self.usage_cache[1]
        rows = []
        with open(self.usage_path) as f:
            for i, line in enumerate(f, 1):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                r["_id"] = i
                rows.append(r)
        self.usage_cache = (key, rows)
        return rows

    def parse_time(self, v):
        if not v:
            return None
        try:
            return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() if (
                "+" in v[10:] or v.endswith("Z")) else datetime.datetime.strptime(v, "%Y-%m-%dT%H:%M").replace(
                tzinfo=zoneinfo.ZoneInfo(self.prefs().get("timezone", TIMEZONE))).timestamp()
        except ValueError:
            return None

    def usage_row(self, r, names):
        w = self.hub.workspaces.by_path(r.get("cwd") or "")
        u = {"input_tokens": r.get("input") or 0, "output_tokens": r.get("output") or 0,
             "cache_read_tokens": r.get("cache_read") or 0, "cache_write_tokens": r.get("cache_write") or 0}
        model = r.get("model") or ""
        price = next((v for k, v in self.prices.items() if model.startswith(k)), None)
        row = {"id": r["_id"], "ts": int(r["ts"] * 1000), "user": OWNER, "session_name": w["name"] if w else
               r.get("session") or "", "session_id": w["id"] if w else "", "thread_id": r.get("sid") or "",
               "turn_id": r.get("turn") or "", "agent": "claude", "account_id": ACCOUNT_ID,
               "account_label": ACCOUNT_LABEL, "model": model, "kind": "chat", "provider": "firstParty",
               "billing": "table" if r.get("cost") is not None else "none", "effort": r.get("effort") or "",
               "effort_control": "effort" if r.get("effort") else "", **u, "total_tokens": sum(u.values()),
               "cost_micro_usd": round((r.get("cost") or 0) * 1e6), "duration_ms": r.get("dur_ms") or 0,
               "wall_ms": r.get("dur_ms") or 0, "ttft_ms": r.get("ttft_ms") or 0,
               "api_ttft_ms": r.get("ttft_ms") or 0, "api_duration_ms": r.get("dur_ms") or 0}
        if price:
            inp, out, cread = price
            row["rate"] = {"input": inp, "output": out, "cache_read": cread, "cache_write": inp * 1.25,
                           "key": next(k for k in self.prices if model.startswith(k)), "basis": "table"}
        return row

    def usage_events(self, q):
        rows = self.usage_all()
        wid = q.get("session")
        path = (self.hub.workspaces.items.get(wid) or {}).get("path") if wid else None
        since, until = self.parse_time(q.get("since")), self.parse_time(q.get("until"))
        model, kind, agent = q.get("model"), q.get("kind"), q.get("agent")
        hit = [r for r in rows if (not wid or r.get("cwd") == path) and (not model or r.get("model") == model)
               and (not kind or kind == "chat") and (not agent or agent == "claude")
               and (since is None or r["ts"] >= since) and (until is None or r["ts"] < until)]
        asc = q.get("order") == "asc"
        hit.sort(key=lambda r: r["ts"], reverse=not asc)
        try:
            limit = min(max(int(q.get("limit") or 100), 1), 500)
        except ValueError:
            limit = 100
        try:
            offset = max(int(q.get("offset") or 0), 0)
        except ValueError:
            offset = 0
        names = {}
        page = [self.usage_row(r, names) for r in hit[offset:offset + limit]]
        total = {"rows": len(hit), "turns": len({r.get("turn") or r["_id"] for r in hit}),
                 "input_tokens": sum(r.get("input") or 0 for r in hit),
                 "output_tokens": sum(r.get("output") or 0 for r in hit),
                 "cache_read_tokens": sum(r.get("cache_read") or 0 for r in hit),
                 "cache_write_tokens": sum(r.get("cache_write") or 0 for r in hit),
                 "cost_micro_usd": round(sum(r.get("cost") or 0 for r in hit) * 1e6)}
        return {"rows": page, "total": total,
                "facets": {"users": [OWNER], "agents": ["claude"], "models": sorted({r.get("model") or "" for r in rows} - {""})},
                "limit": limit, "offset": offset, "order": "asc" if asc else "desc", "scope": "all",
                "timezone": self.prefs().get("timezone", TIMEZONE)}

    def on_hub_event(self, s, ev):
        if ev["kind"] != "usage" or not self.live:
            return
        w = self.hub.workspaces.by_path(s.cwd)
        msg = {"type": "usage", "sessions": [w["id"]] if w else None}
        for q in list(self.live):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    # ---- 设置、监控 ----

    def prefs(self):
        return self.load(self.prefs_path, {})

    def settings(self):
        p = self.prefs()
        res = self.resources()
        pricing = {k: {"input": a, "output": b, "cache_read": c, "cache_write": a * 1.25}
                   for k, (a, b, c) in self.prices.items()}
        return {
            "image_updates": {"enabled": False, "channel": "stable", "time": "04:00", "update_codex": False},
            "resources": {"max_running": 0, "max_running_per_user": 0, "min_free_bytes": 0},
            "listen": "lab", "agent_image": "（主机直跑，不用镜像）", "permission_mode": "bypassPermissions",
            "max_upload_mb": 512, "chat_history_timeout_sec": p.get("chat_history_timeout_sec", 120),
            "idle_timeout_min": 0, "turn_timeout_min": 0, "max_parallel_chats": p.get("max_parallel_chats", 3),
            "timezone": p.get("timezone", TIMEZONE),
            "container": {"memory_mb": res["memory_mb"], "cpus": res["cpus"], "pids_limit": 0, "network": "host"},
            "models": {"claude": [{**m, "reasoning": {"support": "supported", "control": "effort",
                                                      "levels": EFFORT_LEVELS}} for m in MODEL_OPTIONS]},
            "default_models": {"claude": p.get("default_models", {}).get("claude") or DEFAULT_MODEL},
            "terminal_tips": p.get("terminal_tips") or {"tips": [], "interval_sec": 8, "animation": "scroll"},
            "tunnel": {"enabled": False}, "tunnel_active": False, "proxy_bridge": {"bind": ""},
            "pricing": pricing, "restart_required": False,
        }

    def monitor(self):
        (tot0, idle0), pcpu0, t0 = self.cpu_prev
        tot1, idle1 = cpu_times()
        pcpu1, t1 = proc_cpu(), time.time()
        self.cpu_prev = ((tot1, idle1), pcpu1, t1)
        busy = (tot1 - tot0) - (idle1 - idle0)
        cpu = round(100 * busy / (tot1 - tot0), 1) if tot1 > tot0 else 0.0
        mem = mem_info()
        try:
            st = os.statvfs(self.home)
            disk_total, disk_used = st.f_blocks * st.f_frsize, (st.f_blocks - st.f_bfree) * st.f_frsize
        except OSError:
            disk_total = disk_used = 0
        try:
            load1 = os.getloadavg()[0]
        except OSError:
            load1 = 0.0
        rss = 0
        try:
            with open("/proc/self/status") as f:
                rss = next((int(l.split()[1]) * 1024 for l in f if l.startswith("VmRSS:")), 0)
        except OSError:
            pass
        mem_used = mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)
        running = sum(not w.get("stopped", False) for w in self.hub.workspaces.items.values())
        return {"now": int(t1 * 1000), "window_ms": int((t1 - t0) * 1000) if t0 else 0,
                "process": {"cpu_percent": round(100 * (pcpu1 - pcpu0) / (t1 - t0), 1) if t1 > t0 else 0,
                            "rss": rss, "heap_alloc": 0, "heap_sys": 0, "goroutines": threading.active_count(),
                            "uptime_ms": int((t1 - self.started) * 1000)},
                "host": {"cpu_count": os.cpu_count() or 1, "cpu_percent": cpu, "load1": round(load1, 2),
                         "mem_total": mem.get("MemTotal", 0), "mem_used": mem_used,
                         "disk_total": disk_total, "disk_used": disk_used,
                         "disks": [{"roles": ["data"], "total": disk_total, "used": disk_used,
                                    "available": disk_total - disk_used}]},
                # 主机总量留在 host；没有容器，不把整台机器的占用冒充空间资源。
                "summary": {"total": len(self.hub.workspaces.items), "running": running, "cpu_percent": 0,
                            "mem_usage": 0},
                "containers": [],
                "storage": {"quota_at": 0, "budget_bytes": 0, "allocated_bytes": 0, "scanned_at": int(t1 * 1000),
                            "partial": False, "users": []}}

    # ---- 主题 ----

    def themes(self):
        user = self.load(self.themes_path, {})
        views = []
        for m in user.values():
            try:
                manifest = hub_theme.validate({k: v for k, v in m.items() if k != "_updated"})
                views.append({"scope": "user", "manifest": manifest, "updated_at": m.get("_updated", 0)})
            except hub_theme.InvalidTheme:
                continue
        return {"format": 1, "bases": SKIN_BASES,
                "tokens": [{"name": n, "kind": k} for n, k in hub_theme.TOKENS.items()], "site": [], "user": views,
                "can_manage_site": False, "limits": {"site": 0, "user": 20, "bytes": 64 << 10}}


def register(app, api):
    hub = api.hub
    sp: Spaces = hub.spaces
    Error = api.Error

    async def body(request):
        return await api.body(request)

    # ---- 空间 ----

    async def sessions(request):
        items = sorted(hub.workspaces.items.values(), key=lambda w: w.get("created", 0), reverse=True)
        return js([sp.view(w) for w in items])

    async def session_get(request):
        return js(sp.view(sp.ws(request.match_info["id"])))

    async def session_patch(request):
        w = hub.workspaces.rename(request.match_info["id"], (await body(request)).get("name"))
        return js(sp.view(w))

    async def session_delete(request):
        wid = request.match_info["id"]
        sp.ws(wid)
        w = sp.ws(wid)
        if any(s.id in hub.tasks for s in hub.abx.threads_of(w)):
            raise Error("这个空间还有对话在跑，先停掉", status=409)
        hub.workspaces.remove(wid)
        await hub.terminals.kill(wid)
        return js({"ok": True})

    async def session_start(request):
        w = sp.ws(request.match_info["id"])
        sp.start(w)
        return js(sp.view(w))

    async def session_stop(request):
        w = sp.ws(request.match_info["id"])
        await sp.stop(w)
        return js(sp.view(w))

    async def session_create(request):
        import secrets
        c = sp.reserve(secrets.token_hex(16), await body(request))
        c["state"] = "complete"
        sp.save_creations()
        return js(sp.view(sp.ws(c["wid"])), status=201)

    async def switch_account(request):
        w = sp.ws(request.match_info["id"])
        b = await body(request)
        if b.get("account_id") != ACCOUNT_ID:
            raise Error("lab 上只有一个账号：已登录的 Claude Code")
        return js(sp.view(w))

    # ---- 新建空间 ----

    async def creations(request):
        rows = [sp.creation_view(c) for c in sp.creations.values() if c["state"] not in ("complete", "abandoned")]
        return js({"version": 1, "actor_key": sp.actor_key, "creations": rows})

    async def creation_get(request):
        return js(sp.creation_view(sp.creation(request.match_info["request"])))

    async def creation_put(request):
        rid = request.match_info["request"]
        if not CREATION_ID.match(rid):
            raise Error("创建编号不对")
        spec = await body(request)
        async with sp.lock:
            c = sp.creations.get(rid)
            if c:
                if c["state"] == "abandoned" or c.get("wid") not in hub.workspaces.items:
                    from hub_abx import problem
                    return problem(410, "creation_gone", "该创建已放弃或空间已删除")
                if c["request"] != spec:
                    raise Error("这个创建编号已经用来建过别的空间", status=409)
            else:
                c = sp.reserve(rid, spec)
        return js(sp.creation_view(c))

    async def creation_action(request):
        b = await body(request)
        rid = request.match_info["request"]
        if not CREATION_ID.fullmatch(rid):
            raise Error("创建编号不对")
        action = b.get("action")
        c = sp.creations.get(rid)
        if c is None and action == "abandon":
            c = {"request_id": rid, "request": {}, "wid": "", "state": "abandoned", "created_at": now_iso()}
            sp.creations[rid] = c
            sp.save_creations()
            return js({"ok": True})
        c = sp.creation(rid)
        if c["state"] == "abandoned":
            if action == "abandon":
                return js({"ok": True})
            from hub_abx import problem
            return problem(410, "creation_gone", "该创建已经放弃")
        if action == "finish":
            if c.get("wid") not in hub.workspaces.items:
                from hub_abx import problem
                return problem(410, "creation_gone", "该空间已经删除")
            c["state"] = "complete"
        elif action == "abandon":
            w = hub.workspaces.items.get(c.get("wid") or "")
            if w and any(s.id in hub.tasks for s in hub.abx.threads_of(w)):
                raise Error("这个空间还有对话在跑，先停掉", status=409)
            if w and c.get("created_dir") and os.path.isdir(w["path"]) and not any(os.scandir(w["path"])):
                os.rmdir(w["path"])   # 只删自己刚建的空目录
                hub.workspaces.remove(w["id"])
            c["state"] = "abandoned"
        elif action == "review":
            for imp in c.get("imports", []):
                if imp["state"] == "uncertain":
                    imp["state"] = "reviewed"
        else:
            raise Error("不认识的操作")
        sp.save_creations()
        return js(sp.creation_view(c))

    # ---- 账号、引导 ----

    async def accounts(request):
        return js([sp.account()])

    async def no_accounts(request):
        raise Error("lab 上直接用已登录的 Claude Code，不用在这里加账号")

    async def onboarding(request):
        return js({"version": 1, "can_configure": True, "can_create": True,
                   "has_workspaces": bool(hub.workspaces.items),
                   "accounts": [{"id": ACCOUNT_ID, "type": "claude", "label": ACCOUNT_LABEL,
                                 "credentials_present": True, "default_model": DEFAULT_MODEL}],
            "default_models": sp.settings()["default_models"], "container_resources": sp.resources()})

    async def account_usage(request):
        """订阅额度：拿 lab 上 Claude Code 自己的 OAuth 令牌查 /api/oauth/usage（只读，不续期；续期是 CLI 的事）。"""
        sp.ws(request.match_info["id"])
        now = time.time()
        cached = getattr(sp, "quota_cache", None)
        if cached and now - cached[0] < 60:
            return js(cached[1])
        try:
            with open(os.path.join(sp.home, ".claude", ".credentials.json")) as f:
                o = json.load(f).get("claudeAiOauth") or {}
        except (OSError, ValueError):
            raise Error("读不到 lab 上 Claude Code 的登录凭证", status=502)
        if not o.get("accessToken"):
            raise Error("lab 上的 Claude Code 没有用订阅登录", status=502)
        if (o.get("expiresAt") or 0) / 1000 < now:
            raise Error("Claude Code 的登录令牌过期了，发一条消息让它自己续期后再看", status=502)

        def fetch():
            import urllib.request
            req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
                "Authorization": "Bearer " + o["accessToken"], "anthropic-beta": "oauth-2025-04-20",
                "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        try:
            d = await asyncio.to_thread(fetch)
        except Exception as e:
            raise Error(f"查询订阅额度失败：{e}", status=502)
        windows = []
        for key, label in (("five_hour", "5 小时"), ("seven_day", "本周"), ("seven_day_opus", "本周 Opus"),
                           ("seven_day_sonnet", "本周 Sonnet")):
            win = d.get(key) or {}
            if win.get("utilization") is None:
                continue
            windows.append({"key": key, "label": label, "percent": win["utilization"],
                            **({"resets_at": win["resets_at"]} if win.get("resets_at") else {})})
        out = {"account_id": ACCOUNT_ID, "account_label": ACCOUNT_LABEL, "plan": o.get("subscriptionType") or "",
               "windows": windows, "fetched_at": now_iso()}
        ex = d.get("extra_usage") or {}
        if ex.get("is_enabled"):
            scale = 10 ** (ex.get("decimal_places") or 0)
            out["extra"] = {"enabled": True, "percent": ex.get("utilization") or 0,
                            "used": (ex.get("used_credits") or 0) / scale, "limit": (ex.get("monthly_limit") or 0) / scale,
                            **({"currency": ex["currency"]} if ex.get("currency") else {})}
        sp.quota_cache = (now, out)
        return js(out)

    # ---- 使用记录 ----

    async def usage_events(request):
        return js(await asyncio.to_thread(sp.usage_events, request.query), headers={"Cache-Control": "no-store"})

    async def usage_live(request):
        wsr = web.WebSocketResponse(heartbeat=30)
        await wsr.prepare(request)
        q = asyncio.Queue(maxsize=200)
        sp.live.add(q)

        async def writer():
            while True:
                await wsr.send_str(dumps(await q.get()))

        task = asyncio.create_task(writer())
        try:
            async for _ in wsr:
                pass
        finally:
            sp.live.discard(q)
            task.cancel()
        return wsr

    # ---- 设置、系统、监控 ----

    async def settings_get(request):
        return js(sp.settings())

    async def settings_put(request):
        b = await body(request)
        p = sp.prefs()
        # 历史超时与这三个字段属于上游同一保存组，接受原值回传，不假装支持修改。
        fixed = {k: sp.settings()[k] for k in ("permission_mode", "max_upload_mb", "listen")}
        if any(k in b and (type(b[k]) is not type(v) or b[k] != v) for k, v in fixed.items()):
            raise Error("主机版不支持修改权限模式、上传上限或监听地址")
        if "timezone" in b:
            try:
                if not isinstance(b["timezone"], str):
                    raise ValueError
                zoneinfo.ZoneInfo(b["timezone"])
            except (ValueError, zoneinfo.ZoneInfoNotFoundError):
                raise Error("时区无效")
            p["timezone"] = b["timezone"]
        if "terminal_tips" in b:
            tips = b["terminal_tips"]
            if not isinstance(tips, dict) or not isinstance(tips.get("tips"), list) or len(tips["tips"]) > 50 or not all(
                    isinstance(t, str) and len(t) <= 1000 for t in tips["tips"]) or type(tips.get("interval_sec")) is not int or not 0 <= tips["interval_sec"] <= 3600 or tips.get("animation", "scroll") != "scroll":
                raise Error("终端提示格式不对")
            p["terminal_tips"] = {"tips": tips["tips"], "interval_sec": tips["interval_sec"], "animation": "scroll"}
        for key, low, high in (("max_parallel_chats", 1, 10), ("chat_history_timeout_sec", 1, 3600)):
            if key in b:
                value = b[key]
                if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                    raise Error(f"{key} 必须在 {low}–{high} 之间")
                p[key] = value
        if "default_models" in b:
            models = b["default_models"]
            if not isinstance(models, dict) or set(models) - {"claude"} or not isinstance(models.get("claude"), str) or not hub.valid_model(models["claude"]):
                raise Error("默认模型无效")
            p["default_models"] = {"claude": models.get("claude") or DEFAULT_MODEL}
        if set(b) - {"terminal_tips", "max_parallel_chats", "chat_history_timeout_sec", "default_models", "timezone", *fixed}:
            raise Error("主机版不支持这项设置")
        sp.store(sp.prefs_path, p)
        if "max_parallel_chats" in b:
            for wid in hub.abx.rooms:
                hub.abx.broadcast(wid, {"type": "chat_limit", "max_parallel": p["max_parallel_chats"]})
            hub.abx.kick_queue()
        return js(sp.settings())

    async def system(request):
        return js({"version": "agent-hub", "revision": "", "built_at": "", "go_version": "Python",
                   "data_dir": sp.state_dir, "config_path": sp.state_dir, "docker_version": "",
                   "sessions_total": len(hub.workspaces.items),
                   "sessions_running": sum(not w.get("stopped", False) for w in hub.workspaces.items.values()), "accounts": 1, "users": 1,
                   "started_at": int(sp.started * 1000), "listen": "lab"})

    async def monitor(request):
        return js(await asyncio.to_thread(sp.monitor))

    async def storage_quotas(request):
        return js({"enabled": False, "users": []})

    async def users(request):
        return js([{"name": OWNER, "role": "admin", "created_at": iso(sp.started)}])

    async def proxies(request):
        return js({"proxies": [], "revision": 0})

    async def pricing(request):
        return js({"active": {"prices": sp.settings()["pricing"], "revision": "hub", "managed": {},
                              "catalog": {"url": "", "auto_check": False, "auto_apply": False}, "history": []},
                   "candidate": {"catalog": {"schema": 1, "version": "", "published_at": "", "entries": {}},
                                 "revision": "", "url": "", "bundled": False, "checked_at": 0, "attempted_at": 0, "error": ""},
                   "changes": [], "warnings": [], "warnings_truncated": False})

    async def image_updates(request):
        return js({"enabled": False, "supported": False, "reason": "lab 上没有 Agent 镜像，Claude Code 直接装在主机上"})

    async def not_here(request):
        raise Error("lab 单人版没有这个功能")

    # ---- 外壳：主题、通告、升级、隧道、保活、浏览器、扩展 ----

    async def themes(request):
        return js(sp.themes())

    async def theme_validate(request):
        b = await body(request)
        try:
            manifest = hub_theme.validate(b)
        except hub_theme.InvalidTheme as e:
            return js({"error": str(e), "theme_error": e.problem}, status=400)
        return js({"manifest": manifest})

    async def theme_put(request):
        tid = request.match_info["tid"]
        if not THEME_ID.match(tid):
            raise Error("主题 ID 不对")
        b = await body(request)
        try:
            m = hub_theme.validate(b)
        except hub_theme.InvalidTheme as e:
            return js({"error": str(e), "theme_error": e.problem}, status=400)
        if m["id"] != tid:
            raise Error("主题 ID 与路径不一致")
        user = sp.load(sp.themes_path, {})
        if tid not in user and len(user) >= 20:
            raise Error("主题太多了，先删几个")
        user[tid] = {**m, "id": tid, "_updated": int(time.time() * 1000)}
        sp.store(sp.themes_path, user)
        return js({"scope": "user", "manifest": m, "updated_at": user[tid]["_updated"]})

    async def theme_delete(request):
        user = sp.load(sp.themes_path, {})
        user.pop(request.match_info["tid"], None)
        sp.store(sp.themes_path, user)
        return js(sp.themes())

    async def announcement(request):
        return js({"announcement": None, "now": int(time.time() * 1000)})

    async def updates(request):
        return js({"current_version": "agent-hub", "revision": "", "built_at": "", "latest_version": "",
                   "available": False, "comparable": False, "release_url": "", "notes": "", "checked_at": 0,
                   "attempted_at": 0, "error": ""})

    async def upgrade(request):
        return js({"supported": False, "reason": "agent-hub 不走在线升级", "current_version": "agent-hub", "job": None})

    async def components(request):
        if not getattr(sp, "cli_version", None):
            try:
                p = await asyncio.create_subprocess_exec(hub.cli_path, "--version", stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.DEVNULL)
                out, _ = await asyncio.wait_for(p.communicate(), 20)
                sp.cli_version = out.decode().split()[0] if out.strip() else "未知"
            except (OSError, asyncio.TimeoutError):
                sp.cli_version = "未知"
        now = int(time.time() * 1000)
        return js({"version": 1, "observed_at": now,
                   "server": {"version": "agent-hub", "schema": 1, "candidate_compatibility": "preflight_required"},
                   "image": {"reference": "lab 主机直跑（不用镜像）", "id": "", "claude": sp.cli_version, "codex": "",
                             "state": "labels_only", "observed_at": now},
                   "desktop": {"installed_version_state": "browser_unknown", "server_protocol": 0,
                               "sync_enabled": False}})

    async def tunnel_status(request):
        return js({"enabled": False, "proxy_up": False, "connected": False, "maps": []})

    async def keepalive(request):
        zero = {"max_hours": 0, "min_context_tokens": 0}
        return js({"available": False, "reason": "disabled", "enabled": False, "terminal": False, **zero,
                   "defaults": zero, "effective": zero, "max_hours_limit": 0, "interval_minutes": 0, "plans": []})

    async def browser(request):
        if request.method == "GET":
            return js({"available": False, "running": False, "proxy": False})
        raise Error("lab 上没装远程浏览器")

    async def extensions(request):
        return js({"extensions": [], "revision": 0})

    async def empty_list(request):
        return js([])

    async def delegation_options(request):
        return js({"available": False, "workers": []})

    r = app.router
    r.add_get("/api/sessions", sessions)
    r.add_post("/api/sessions", session_create)
    r.add_get("/api/sessions/{id}", session_get)
    r.add_patch("/api/sessions/{id}", session_patch)
    r.add_delete("/api/sessions/{id}", session_delete)
    r.add_post("/api/sessions/{id}/start", session_start)
    r.add_post("/api/sessions/{id}/stop", session_stop)
    r.add_put("/api/sessions/{id}/account", switch_account)
    r.add_get("/api/sessions/{id}/account/usage", account_usage)
    r.add_get("/api/session-creations", creations)
    r.add_get("/api/session-creations/{request}", creation_get)
    r.add_put("/api/session-creations/{request}", creation_put)
    r.add_post("/api/session-creations/{request}", creation_action)
    r.add_get("/api/accounts", accounts)
    for method, path in (("POST", "/api/accounts"), ("PATCH", "/api/accounts/{aid}"), ("DELETE", "/api/accounts/{aid}"),
                         ("POST", "/api/accounts/{aid}/oauth/start"), ("POST", "/api/accounts/{aid}/oauth/finish"),
                         ("POST", "/api/accounts/{aid}/apikey"), ("DELETE", "/api/accounts/{aid}/apikey"),
                         ("POST", "/api/accounts/{aid}/apikey/test"), ("POST", "/api/accounts/{aid}/models/discover")):
        r.add_route(method, path, no_accounts)
    r.add_get("/api/onboarding", onboarding)
    r.add_get("/api/usage/events", usage_events)
    r.add_get("/api/usage/live", usage_live)
    r.add_get("/api/settings", settings_get)
    r.add_put("/api/settings", settings_put)
    r.add_get("/api/system", system)
    r.add_get("/api/monitor", monitor)
    r.add_get("/api/storage/quotas", storage_quotas)
    r.add_get("/api/users", users)
    r.add_get("/api/proxies", proxies)
    r.add_get("/api/pricing", pricing)
    r.add_get("/api/image-updates", image_updates)
    for method, path in (("POST", "/api/users"), ("POST", "/api/proxies"), ("PUT", "/api/pricing"),
                         ("POST", "/api/image-updates/{action}"), ("POST", "/api/tunnel/pair"),
                         ("PUT", "/api/announcement"), ("POST", "/api/updates/upgrade"),
                         ("POST", "/api/me/password"), ("PUT", "/api/sessions/{id}/keepalive"),
                         ("POST", "/api/sessions/{id}/keepalive/stop")):
        r.add_route(method, path, not_here)
    r.add_get("/api/themes", themes)
    r.add_post("/api/themes/validate", theme_validate)
    r.add_put("/api/themes/user/{tid}", theme_put)
    r.add_delete("/api/themes/user/{tid}", theme_delete)
    r.add_get("/api/announcement", announcement)
    r.add_get("/api/updates", updates)
    r.add_post("/api/updates/check", updates)
    r.add_get("/api/updates/upgrade", upgrade)
    r.add_get("/api/updates/components", components)
    r.add_get("/api/tunnel/status", tunnel_status)
    r.add_get("/api/tunnel/clients", empty_list)
    r.add_get("/api/sessions/{id}/keepalive", keepalive)
    r.add_route("*", "/api/sessions/{id}/browser", browser)
    r.add_get("/api/extensions", extensions)
    r.add_get("/api/sessions/{id}/delegation/options", delegation_options)
    async def delegation_stats(request):
        return js({"stats": []})
    r.add_get("/api/delegation/stats", delegation_stats)
