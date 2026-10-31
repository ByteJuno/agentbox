"""
Git（2026-10-27）：agentbox 前端的「变更」页——仓库状态、diff、整文件、提交、丢弃。接口格式照 agentbox 的
internal/server/git.go。远程（获取 / 拉取 / 推送）、分支、PR、克隆这些对话框 10-27 按机主的意思删了，
入口在 web/hub/hub.css 里藏起来；要做这些让 Claude 用本机的 git / gh 做，或者在终端里做。

提交身份：仓库自己配了 user.name / user.email（或者全局配了）就用 git 自己的；都没配时才用网页上
「Git 管理 → 提交身份」保存的那份（默认 ByteJuno 的 GitHub noreply 邮箱）。不改你的全局 git 配置。
"""
import asyncio
import datetime
import json
import os
import time
import urllib.parse

from aiohttp import web

SCAN_DEPTH, SCAN_MAX = 2, 20
SCAN_SKIP = {"node_modules", "__MACOSX", "vendor"}
MAX_STATUS_FILES = 2000
MAX_FILE_VIEW = 512 << 10
HOST_CONN = "host"
DEFAULT_PROFILE = {"name": "ByteJuno", "email": "87103000+ByteJuno@users.noreply.github.com"}


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class GitError(Exception):
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


def display_url(raw):
    """remote 地址给页面看：去掉里面可能夹着的账号密码。"""
    try:
        u = urllib.parse.urlsplit(raw)
        if u.scheme in ("http", "https", "ssh", "git") and u.hostname:
            host = u.hostname + (f":{u.port}" if u.port else "")
            return urllib.parse.urlunsplit((u.scheme, host, u.path, "", ""))
    except ValueError:
        pass
    if "://" not in raw and ":" in raw:
        host, _, path = raw.rpartition("@")[2].partition(":")
        if host and path and "/" not in host:
            return host + ":" + path
    return "（本地路径或未识别的远程地址）"


def parse_status(out):
    st = {"branch": "", "head": "", "detached": False, "unborn": False, "upstream": "", "ahead": 0, "behind": 0,
          "tracking_known": False}
    files = []
    records = out.split("\x00")
    i = 0
    while i < len(records):
        line = records[i]
        if line.startswith("# branch.oid "):
            st["head"] = line[13:]
            if st["head"] == "(initial)":
                st["unborn"], st["head"] = True, ""
        elif line.startswith("# branch.head "):
            st["branch"] = line[14:]
            if st["branch"] == "(detached)":
                st["detached"], st["branch"] = True, ""
        elif line.startswith("# branch.upstream "):
            st["upstream"] = line[18:]
        elif line.startswith("# branch.ab "):
            parts = line[12:].split()
            if len(parts) == 2:
                try:
                    st["ahead"], st["behind"] = int(parts[0].lstrip("+")), int(parts[1].lstrip("-"))
                    st["tracking_known"] = True
                except ValueError:
                    pass
        elif line.startswith("? "):
            files.append({"path": line[2:], "status": "??", "untracked": True})
        elif line[:2] in ("1 ", "2 ", "u "):
            n = {"1": 9, "2": 10, "u": 11}[line[0]]
            parts = line.split(" ", n - 1)
            if len(parts) == n:
                files.append({"path": parts[n - 1], "status": parts[1].replace(".", " "), "untracked": False})
            if line[0] == "2":
                i += 1   # 改名记录后面跟着原路径
        i += 1
    return st, files


class Git:
    def __init__(self, hub, state_dir, error):
        self.hub = hub
        self.Error = error
        self.profile_path = os.path.join(state_dir, "git-profile.json")
        self.ops: list[dict] = []                   # 最近的 Git 操作记录（「记录」对话框）
        self.locks: dict[str, asyncio.Lock] = {}

    # ---- 基础 ----

    def env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_HUB")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
                   GIT_SSH_COMMAND=os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes"))
        return env

    async def run(self, cwd, *args, timeout=20, ok_codes=(0,), cmd="git"):
        argv = [cmd, *args] if cmd != "git" else ["git", "-C", cwd, *args]
        p = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=self.env(), stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()
            raise GitError(f"{cmd} {args[0] if args else ''} 超时（{timeout} 秒）")
        out, err = out.decode(errors="replace"), err.decode(errors="replace")
        if p.returncode not in ok_codes:
            raise GitError((err.strip() or out.strip() or f"{cmd} 退出码 {p.returncode}")[-1500:], p.returncode)
        return out

    def lock(self, d):
        return self.locks.setdefault(d, asyncio.Lock())

    def record(self, wid, repo, op, target, result, started):
        self.ops.insert(0, {"id": int(started * 1000), "started_at": datetime.datetime.fromtimestamp(started).astimezone()
                            .isoformat(timespec="seconds"), "finished_at": now_iso(), "actor": "lab", "session_id": wid,
                            "repo": repo, "connection_id": HOST_CONN, "operation": op, "target": target,
                            "result": result})
        del self.ops[200:]

    async def op(self, w, repo, name, target, coro):
        started = time.time()
        try:
            res = await coro
        except BaseException:
            self.record(w["id"], repo, name, target, "failed", started)
            raise
        self.record(w["id"], repo, name, target, "success", started)
        return res

    # ---- 仓库 ----

    @staticmethod
    def has_git(d):
        return os.path.exists(os.path.join(d, ".git"))

    def repos(self, ws):
        out = [""] if self.has_git(ws) else []

        def walk(d, rel, depth):
            if depth > SCAN_DEPTH or len(out) >= SCAN_MAX:
                return
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError:
                return
            for e in entries:
                if e.name.startswith(".") or e.name in SCAN_SKIP or not e.is_dir(follow_symlinks=False):
                    continue
                sub_rel = f"{rel}/{e.name}" if rel else e.name
                if self.has_git(e.path):
                    out.append(sub_rel)
                else:
                    walk(e.path, sub_rel, depth + 1)
                if len(out) >= SCAN_MAX:
                    return

        walk(ws, "", 1)
        return sorted(out)

    def pick(self, w, want, strict=True):
        roots = self.repos(w["path"])
        if not roots:
            raise self.Error("工作区里没有 Git 仓库")
        want = (want or "").strip("/")
        if not want:
            rel = roots[0]
        elif want in roots:
            rel = want
        elif strict:   # 提交、丢弃是破坏性操作，不认识的仓库绝不能悄悄换成别的
            raise self.Error("仓库不存在：" + want)
        else:
            rel = roots[0]
        return rel, os.path.join(w["path"], rel) if rel else w["path"], roots

    @staticmethod
    def rel_path(p):
        p = (p or "").replace("\\", "/")
        norm = os.path.normpath(p) if p else ""
        if p and (os.path.isabs(p) or norm == ".." or norm.startswith("../")):
            raise ValueError
        return norm if p else ""

    async def remotes(self, d):
        out = await self.run(d, "config", "--local", "--null", "--get-regexp", r"^remote\..*\.(url|pushurl)$",
                             ok_codes=(0, 1))
        res = []
        for rec in out.split("\x00"):
            key, _, value = rec.partition("\n")
            if not key.startswith("remote."):
                continue
            push = key.endswith(".pushurl")
            name = key[len("remote."):-len(".pushurl" if push else ".url")]
            res.append({"name": name, "url": display_url(value), "push": push, "raw": value})
        return res

    async def status(self, w, want):
        roots = self.repos(w["path"])
        if not roots:
            return {"is_repo": False, "repos": []}
        rel, d, roots = self.pick(w, want, strict=False)
        out = await self.run(d, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all")
        st, files = parse_status(out)
        remotes = [{k: r[k] for k in ("name", "url", "push")} for r in await self.remotes(d)]
        return {"is_repo": True, "repo": rel, "repos": roots, **st,
                "last_fetch": None, "remotes": remotes,
                "files": files[:MAX_STATUS_FILES], "truncated": len(files) > MAX_STATUS_FILES}

    # ---- 提交身份 ----

    def profile(self):
        try:
            with open(self.profile_path) as f:
                p = json.load(f)
            return {"name": p.get("name") or "", "email": p.get("email") or "", "updated_at": p.get("updated_at") or ""}
        except (FileNotFoundError, ValueError):
            return {**DEFAULT_PROFILE, "updated_at": ""}

    def save_profile(self, name, email):
        p = {"name": name, "email": email, "updated_at": now_iso()}
        tmp = self.profile_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(p, f, ensure_ascii=False)
        os.replace(tmp, self.profile_path)
        return p

    async def identity(self, d):
        """仓库（含全局）配置的提交身份；没配返回 None。"""
        name = (await self.run(d, "config", "user.name", ok_codes=(0, 1))).strip()
        email = (await self.run(d, "config", "user.email", ok_codes=(0, 1))).strip()
        return (name, email) if name and email else None


def register(app, api):
    hub = api.hub
    g: Git = hub.git
    Error = api.Error

    def js(data, status=200):
        return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))

    def ws(request):
        return hub.workspaces.get(request.match_info["id"])

    async def body(request):
        try:
            b = await request.json()
        except ValueError:
            raise Error("invalid request body")
        return b if isinstance(b, dict) else {}

    def wrap(fn):
        async def handler(request):
            try:
                return await fn(request)
            except GitError as e:
                return js({"ok": False, "error": str(e)}, status=400)
        return handler

    async def status(request):
        return js(await g.status(ws(request), request.query.get("repo")))

    async def diff(request):
        w = ws(request)
        _, d, _ = g.pick(w, request.query.get("repo"))
        try:
            p = g.rel_path(request.query.get("path"))
        except ValueError:
            raise Error("invalid path")
        has_head = (await g.run(d, "rev-parse", "--verify", "--quiet", "HEAD", ok_codes=(0, 1))).strip()
        args = ["diff", "--no-ext-diff", "--no-textconv", "HEAD" if has_head else "--cached", "--"]
        out = await g.run(d, *args, *([p] if p else []))
        return web.Response(text=out, content_type="text/plain", charset="utf-8")

    async def file(request):
        w = ws(request)
        _, d, _ = g.pick(w, request.query.get("repo"))
        try:
            p = g.rel_path(request.query.get("path"))
        except ValueError:
            raise Error("invalid path")
        full = os.path.join(d, p)
        if not p or not os.path.isfile(full):
            raise Error("文件或目录不存在", status=404)
        size = os.path.getsize(full)
        if size > MAX_FILE_VIEW:
            raise Error(f"文件太大（{size / (1 << 20):.1f} MB），请到「文件」页打开")
        with open(full, "rb") as f:
            raw = f.read()
        if b"\x00" in raw:
            raise Error("二进制文件，无法按文本查看")
        try:
            text = raw.decode()
        except UnicodeDecodeError:
            raise Error("二进制文件，无法按文本查看")
        return web.Response(text=text, content_type="text/plain", charset="utf-8")

    async def commit(request):
        w = ws(request)
        b = await body(request)
        msg = (b.get("message") or "").strip()
        if not msg:
            raise Error("提交信息不能为空")
        rel, d, _ = g.pick(w, b.get("repo"))

        async def go():
            async with g.lock(d):
                await g.run(d, "add", "-A")
                ident = await g.identity(d)
                prof = g.profile()
                extra = [] if ident else ["-c", f"user.name={prof['name']}", "-c", f"user.email={prof['email']}"]
                try:
                    out = await g.run(d, *extra, "commit", "-m", msg, timeout=60)
                except GitError as e:
                    raise Error("提交失败：" + str(e))
                sha = (await g.run(d, "rev-parse", "--verify", "HEAD", ok_codes=(0, 128))).strip()
                warning = ""
                if ident and (ident[0], ident[1]) != (prof["name"], prof["email"]):
                    warning = f"已提交到本地 · {sha[:12]}，未推送；提交身份按这个仓库的配置：{ident[0]} <{ident[1]}>"
                return {"output": out.strip(), "sha": sha, "pushed": False, "warning": warning}

        return js(await g.op(w, rel, "commit", "HEAD", go()))

    async def discard(request):
        w = ws(request)
        b = await body(request)
        rel, d, _ = g.pick(w, b.get("repo"))
        try:
            target = g.rel_path(b.get("path")) or "."
        except ValueError:
            raise Error("invalid path")

        async def go():
            async with g.lock(d):
                tracked = await g.run(d, "ls-files", "-z", "--", target)
                if tracked:
                    await g.run(d, "checkout", "HEAD", "--", target)
                await g.run(d, "clean", "-fd", "--", target)
            return {"ok": True}

        return js(await g.op(w, rel, "discard", target, go()))

    # ---- 用户级：连接、默认、身份、记录 ----

    async def connections(request):
        return js([])   # 新建空间对话框会读；lab 用本机凭证，没有网页上的连接

    async def default(request):
        return js({"connection_id": ""})

    async def me_git(request):
        if request.method == "PUT":
            b = await body(request)
            name, email = (b.get("name") or "").strip()[:128], (b.get("email") or "").strip()[:254]
            if not name or "@" not in email:
                raise Error("姓名和邮箱都要填，邮箱要像个邮箱")
            p = g.save_profile(name, email)
        else:
            p = g.profile()
        return js({"user": "lab", **p})

    r = app.router
    p = "/api/sessions/{id}/git"
    for method, path, fn in [("GET", "/status", status), ("GET", "/diff", diff), ("GET", "/file", file),
                             ("POST", "/commit", commit), ("POST", "/discard", discard)]:
        r.add_route(method, p + path, wrap(fn))
    r.add_get("/api/git/connections", connections)
    r.add_get("/api/me/git/default", default)
    r.add_get("/api/me/git", me_git)
    r.add_put("/api/me/git", me_git)
