"""
工作空间（2026-10-26）：一个项目目录一个空间，空间里可以有多个对话（Claude Code 会话），照 agentbox 的组织方式。

空间登记在 ~/.agent-hub/workspaces.json，会话按 cwd 归到空间。新建空间可以选 home 下已有的目录、
新建一个空目录，或者 git clone 一个仓库（后台跑，进度推到 /api/stream）。删除空间只是不再登记，不动文件。
输入框里敲 @ 时的文件搜索也在这里：git 仓库用 git ls-files，否则遍历目录（跳过依赖和构建目录）。
"""
import asyncio
import json
import os
import re
import secrets
import time

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "build", "dist", ".gradle", ".idea",
             "target", ".next", ".cache", ".mypy_cache", ".pytest_cache"}
NAME_RE = re.compile(r"^[\w.\-]{1,80}$")
GIT_URL_RE = re.compile(r"^(https://[\w.\-]+/[\w.\-/~]+|git@[\w.\-]+:[\w.\-/~]+)$")


def git_branch(path):
    try:
        with open(os.path.join(path, ".git", "HEAD")) as f:
            head = f.read().strip()
    except OSError:
        return None
    return head.removeprefix("ref: refs/heads/") if head.startswith("ref:") else head[:8]


class Workspaces:
    def __init__(self, hub, state_dir, home, error):
        self.hub = hub
        self.path = os.path.join(state_dir, "workspaces.json")
        self.home = home
        self.Error = error
        self.items: dict[str, dict] = {}
        self.cloning: dict[str, str] = {}        # 空间 id → clone 进度的最后一行
        self.file_cache: dict[str, tuple] = {}   # 目录 → (时间, 文件列表)
        try:
            with open(self.path) as f:
                self.items = {w["id"]: w for w in json.load(f)}
        except FileNotFoundError:
            pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(list(self.items.values()), f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def by_path(self, path):
        return next((w for w in self.items.values() if w["path"] == path), None)

    def get(self, wid):
        w = self.items.get(wid)
        if not w:
            raise self.Error("没有这个工作空间", status=404)
        return w

    def ensure(self, path, name=None):
        w = self.by_path(path)
        if w is None:
            wid = secrets.token_hex(4)
            while wid in self.items:
                wid = secrets.token_hex(4)
            w = {"id": wid, "name": name or os.path.basename(path.rstrip("/")) or path, "path": path,
                 "created": time.time()}
            self.items[wid] = w
            self.save()
        return w

    def sync_from_sessions(self):
        for s in list(self.hub.sessions.values()):
            self.ensure(s.cwd)

    def inside_home(self, path):
        return path == self.home or path.startswith(self.home + os.sep)

    def view(self, w):
        sessions = sorted((s for s in self.hub.sessions.values() if s.cwd == w["path"]),
                          key=lambda s: s.updated, reverse=True)
        states = {s.status for s in sessions}
        status = next((st for st in ("running", "waiting", "error") if st in states), "idle")
        if w["id"] in self.cloning:
            status = "cloning"
        return {**w, "status": status, "exists": os.path.isdir(w["path"]), "branch": git_branch(w["path"]),
                "updated": max([w["created"]] + [s.updated for s in sessions]),
                "waiting": sum(len(self.hub.waiters_of(s)) for s in sessions),
                "clone_progress": self.cloning.get(w["id"]),
                "sessions": [self.hub.view(s) for s in sessions]}

    def list(self):
        return sorted((self.view(w) for w in self.items.values()), key=lambda v: v["updated"], reverse=True)

    def target(self, parent, dirname):
        parent = os.path.realpath(os.path.expanduser(parent or os.path.join(self.home, "ai-work")))
        if not self.inside_home(parent) or not os.path.isdir(parent):
            raise self.Error("上级目录要在 home 下面且已存在")
        if not NAME_RE.match(dirname or ""):
            raise self.Error("目录名只能用字母、数字、中文、点、横线和下划线")
        path = os.path.join(parent, dirname)
        if os.path.exists(path):
            raise self.Error(f"{path} 已经存在")
        if self.by_path(path):
            raise self.Error("已经有这个工作空间了")
        return path

    async def create(self, req):
        kind = req.get("kind") or "existing"
        name = (req.get("name") or "").strip()[:60] or None
        if kind == "existing":
            path = os.path.realpath(os.path.expanduser(req.get("path") or ""))
            if not self.inside_home(path) or not os.path.isdir(path):
                raise self.Error("只能选 home 下面已有的目录")
            return self.ensure(path, name)
        if kind == "new":
            path = self.target(req.get("parent"), (req.get("dirname") or "").strip())
            os.makedirs(path)
            return self.ensure(path, name)
        if kind == "git":
            url = (req.get("git_url") or "").strip()
            if not GIT_URL_RE.match(url):
                raise self.Error("仓库地址要是 https://… 或 git@…:…")
            dirname = (req.get("dirname") or "").strip() or url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
            path = self.target(req.get("parent"), dirname)
            w = self.ensure(path, name or dirname)
            self.cloning[w["id"]] = "准备克隆…"
            asyncio.create_task(self.clone(w, url, path))
            return w
        raise self.Error(f"不认识的新建方式 {kind}")

    async def clone(self, w, url, path):
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--progress", url, path, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        tail = ""
        while True:
            chunk = await proc.stdout.read(1024)
            if not chunk:
                break
            text = chunk.decode(errors="replace").replace("\r", "\n").strip().splitlines()
            if text:
                tail = text[-1][:200]
                self.cloning[w["id"]] = tail
                self.hub.broadcast("workspace", id=w["id"], progress=tail)
        code = await proc.wait()
        self.cloning.pop(w["id"], None)
        if code != 0:
            self.items.pop(w["id"], None)
            self.save()
            self.hub.broadcast("workspace", id=w["id"], error=f"克隆失败：{tail}")
        else:
            self.hub.broadcast("workspace", id=w["id"], done=True)

    def rename(self, wid, name):
        w = self.get(wid)
        name = (name or "").strip()[:60]
        if not name:
            raise self.Error("名字不能是空的")
        w["name"] = name
        self.save()
        self.hub.broadcast("workspace", id=wid)
        return w

    def remove(self, wid):
        w = self.get(wid)
        sessions = [s for s in self.hub.sessions.values() if s.cwd == w["path"]]
        if any(s.id in self.hub.tasks for s in sessions):
            raise self.Error("这个空间还有对话在跑，先停掉", status=409)
        if hasattr(self.hub, "abx"):
            self.hub.abx.delete_receipts(wid)
        for s in sessions:   # 只从 hub 里移除；Claude Code 自己的会话记录还在，以后还能再接手
            self.hub.sessions.pop(s.id, None)
            try:
                os.unlink(self.hub.log_of(s.id).path)
            except FileNotFoundError:
                pass
            self.hub.logs.pop(s.id, None)
        self.hub.save()
        del self.items[wid]
        self.save()
        self.hub.broadcast("workspace", id=wid, removed=True)

    # ---- @ 文件搜索 ----

    async def files(self, root):
        cached = self.file_cache.get(root)
        if cached and time.time() - cached[0] < 30:
            return cached[1]
        files = []
        if os.path.isdir(os.path.join(root, ".git")):
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", root, "ls-files", "-co", "--exclude-standard", stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
            files = out.decode(errors="replace").splitlines()[:50000]
        if not files:
            files = await asyncio.to_thread(self.walk, root)
        self.file_cache[root] = (time.time(), files)
        return files

    @staticmethod
    def walk(root):
        out = []
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
            rel = os.path.relpath(d, root)
            out.extend(n if rel == "." else f"{rel}/{n}" for n in names)
            if len(out) > 20000:
                break
        return out

    async def search(self, wid, q, limit=30):
        w = self.get(wid)
        files = await self.files(w["path"])
        q = (q or "").lower().strip()
        if not q:
            return files[:limit]
        scored = []
        for f in files:
            fl = f.lower()
            base = fl.rsplit("/", 1)[-1]
            if base.startswith(q):
                score = 0
            elif q in base:
                score = 1
            elif q in fl:
                score = 2
            else:   # 子序列匹配：ProjList → ProjectList
                it = iter(fl)
                if not all(c in it for c in q):
                    continue
                score = 3
            scored.append((score, len(f), f))
        scored.sort()
        return [f for _, _, f in scored[:limit]]


def register(app, api):
    ws = api.hub.workspaces

    async def list_(request):
        return api.ok({"workspaces": ws.list()})

    async def create(request):
        w = await ws.create(await api.body(request))
        return api.ok({"workspace": ws.view(w)})

    async def patch(request):
        b = await api.body(request)
        return api.ok({"workspace": ws.view(ws.rename(request.match_info["wid"], b.get("name")))})

    async def delete(request):
        ws.remove(request.match_info["wid"])
        return api.ok({})

    async def search(request):
        return api.ok({"files": await ws.search(request.match_info["wid"], request.query.get("q"))})

    app.router.add_get("/hub/workspaces", list_)
    app.router.add_post("/hub/workspaces", create)
    app.router.add_patch("/hub/workspaces/{wid}", patch)
    app.router.add_delete("/hub/workspaces/{wid}", delete)
    app.router.add_get("/hub/workspaces/{wid}/search", search)
